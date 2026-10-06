"""Поиск в интернете для агента: Yandex Search API v2 (web_search) и чтение страницы (read_page).

Порядок для агента — сначала база проекта, интернет — только если там ответа нет и вопрос про платформу,
типовую конфигурацию, ошибку или версию (см. SYSTEM_PROMPT в agent/tools.py).

Что уходит наружу (согласовано): тексты ошибок 1С, имена объектов метаданных, номера версий, общие
формулировки. Не уходят: имена людей, e-mail и телефоны, серверы и внутренние адреса, название заказчика.
Это проверяется в коде (sanitize_query), а не только в подсказке модели: запрещённое вырезается, слишком
пустой после очистки запрос не отправляется. Каждый запрос пишется в журнал .cache/web/web_search.jsonl —
что спросил агент и что ушло на самом деле.

read_page читает только внешние http(s)-страницы: адреса локальной сети, localhost и внутренние домены
отклоняются до запроса (агент не должен ходить внутрь контура по ссылке со страницы).
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

SEARCH_URL = "https://searchapi.api.cloud.yandex.net/v2/web/search"
# Сайты, которые агент может предпочесть для вопросов по 1С (подсказка, а не ограничение)
PREFERRED_SITES = ("its.1c.ru", "v8.1c.ru", "infostart.ru", "partners.v8.1c.ru", "forum.mista.ru", "1c-dn.com")
PAGE_MAX_BYTES = 2 * 1024 * 1024
_LOCK = threading.Lock()

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)|\+\d[\d\s().-]{8,}\d")
_SERVER = re.compile(r"\b[a-z][a-z0-9-]{2,}(?:app|srv|sql|db|web|ras|apl|dc|fs)[0-9]{1,3}\b", re.IGNORECASE)
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_UNC = re.compile(r"\\\\[\w.-]+(?:\\[^\s\\]+)*")
_HOST_WORDS = re.compile(r"\b[\w-]+\.(?:lcl|local|lan|corp|internal)\b", re.IGNORECASE)


class WebError(Exception):
    """Запрос не отправлен или страница не прочитана — причина текстом для агента."""


def blocked_terms(settings) -> list[str]:
    """Что нельзя отправлять: свои домены и их «имена» (pierre-fabre → «pierre fabre», «pierre-fabre»), аналитики,
    плюс COPILOT_WEB_BLOCKED_TERMS (например, «Pierre Fabre», «Пьер Фабр»)."""
    out: list[str] = []
    for d in getattr(settings, "internal_domains", ()) or ():
        base = d.split(".")[0]
        out += [d, base, base.replace("-", " ")]
    for name in getattr(settings, "analysts", ()) or ():
        out += [p.strip(" .,") for p in name.split() if len(p.strip(" .,")) >= 3]
    out += list(getattr(settings, "web_blocked_terms", ()) or ())
    return sorted({t for t in out if len(t) >= 3}, key=len, reverse=True)


def sanitize_query(query: str, settings) -> tuple[str, list[str]]:
    """(очищенный запрос, что вырезано). Пустой запрос после очистки — WebError: отправлять нечего."""
    q = " ".join((query or "").split())
    removed: list[str] = []

    def cut(pattern: re.Pattern, label: str) -> None:
        nonlocal q
        found = pattern.findall(q)
        if found:
            removed.extend(f"{label}: {f}" for f in found)
            q = pattern.sub(" ", q)

    cut(_EMAIL, "e-mail")
    cut(_PHONE, "телефон")
    cut(_UNC, "сетевой путь")
    cut(_HOST_WORDS, "внутренний адрес")
    cut(_SERVER, "сервер")

    def private_ip(m: re.Match) -> str:
        """Только адреса внутренних сетей: номера версий (11.5.27.75) выглядят как IP, но остаются."""
        try:
            ip = ipaddress.ip_address(m.group(0))
        except ValueError:
            return m.group(0)
        if ip.is_private or ip.is_loopback or ip.is_link_local:
            removed.append(f"IP-адрес: {m.group(0)}")
            return " "
        return m.group(0)

    q = _IP.sub(private_ip, q)
    for term in blocked_terms(settings):
        pat = re.compile(rf"(?<![\wЁё]){re.escape(term)}(?![\wЁё])", re.IGNORECASE)
        if pat.search(q):
            removed.append(f"запрещённое слово: {term}")
            q = pat.sub(" ", q)
    q = " ".join(q.split()).strip(" ,;:.-")[:400]
    if len(re.sub(r"\W", "", q)) < 4:
        raise WebError("после удаления служебных и персональных данных в запросе ничего не осталось — "
                       "сформулируй его через текст ошибки, объекты 1С и версии")
    return q, removed


def journal(settings, record: dict[str, Any]) -> None:
    path = Path(getattr(settings, "cache_dir", "") or ".cache") / "web" / "web_search.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"at": datetime.now().astimezone().isoformat(timespec="seconds"), **record},
                          ensure_ascii=False)
        with _LOCK, path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # журнал не должен ломать ответ


# ---------- поиск ----------

def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def parse_results(xml: bytes, k: int) -> list[dict[str, str]]:
    """XML Search API (yandexsearch/response/results/grouping/group/doc) → [{title, url, domain, snippet}]."""
    from lxml import etree

    root = etree.fromstring(xml, parser=etree.XMLParser(recover=True, resolve_entities=False, no_network=True))
    if root is None:
        return []
    err = root.find(".//response/error")
    if err is not None:
        code = err.get("code", "")
        if code == "15":  # «Искомая комбинация слов нигде не встречается»
            return []
        raise WebError(f"Search API: {_text(err)} (код {code})")
    out = []
    for doc in root.iter("doc"):
        passages = [_text(p) for p in doc.findall("./passages/passage")]
        snippet = " … ".join(p for p in passages if p) or _text(doc.find("headline"))
        url = _text(doc.find("url"))
        if url:
            out.append({"title": _text(doc.find("title")) or url, "url": url,
                        "domain": _text(doc.find("domain")) or urlparse(url).netloc, "snippet": snippet[:600]})
        if len(out) >= k:
            break
    return out


def web_search(query: str, settings, sites: list[str] | None = None, k: int = 5,
               client: httpx.Client | None = None) -> dict[str, Any]:
    """Поиск. Возвращает {query_sent, removed, results[]}; в журнал — исходный и отправленный запрос."""
    if not (settings.yc_api_key and settings.yc_folder_id):
        raise WebError("нет ключа AI Studio — поиск в интернете недоступен")
    sent, removed = sanitize_query(query, settings)
    clean_sites = [s.strip().lower() for s in sites or [] if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", s.strip().lower())]
    text = sent + (" (" + " | ".join(f"site:{s}" for s in clean_sites[:5]) + ")" if clean_sites else "")
    # Формат полей — как в примере документации Yandex (sync web search v2): короткие значения перечислений
    body = {"query": {"searchType": "ru", "queryText": text[:400], "familyMode": "strict", "page": "0"},
            "groupSpec": {"groupsOnPage": str(min(max(k, 1), 10))},
            "maxPassages": "3", "region": "225", "l10N": "ru", "folderId": settings.yc_folder_id,
            "responseFormat": "XML"}
    c = client or httpx.Client(timeout=20)
    try:
        r = c.post(SEARCH_URL, json=body, headers={"Authorization": f"Api-Key {settings.yc_api_key}"})
    except httpx.HTTPError as exc:
        journal(settings, {"query": query, "sent": text, "removed": removed, "error": type(exc).__name__})
        raise WebError(f"Search API недоступен: {type(exc).__name__}") from exc
    finally:
        if client is None:
            c.close()
    if r.status_code != 200:
        journal(settings, {"query": query, "sent": text, "removed": removed, "error": f"HTTP {r.status_code}"})
        raise WebError(f"Search API: HTTP {r.status_code} {r.text[:200]}")
    raw = base64.b64decode(r.json().get("rawData", "") or b"")
    results = parse_results(raw, k)
    journal(settings, {"query": query, "sent": text, "removed": removed, "results": [x["url"] for x in results]})
    return {"query_sent": text, "removed": removed, "results": results}


# ---------- чтение страницы ----------

def _is_public_host(host: str, settings) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host or host == "localhost" or "." not in host:
        return False
    if any(host == d or host.endswith("." + d) for d in getattr(settings, "internal_domains", ()) or ()):
        return False
    if re.search(r"\.(lcl|local|lan|corp|internal)$", host):
        return False
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return False
    return True


def html_text(html: bytes, encoding: str | None) -> tuple[str, str]:
    """(заголовок, текст) страницы без скриптов, стилей и навигации."""
    from lxml import html as lh

    doc = lh.document_fromstring(html.decode(encoding or "utf-8", errors="replace") or "<html/>")
    for bad in doc.xpath("//script|//style|//noscript|//nav|//footer|//header|//form|//svg"):
        bad.drop_tree()
    title = " ".join((doc.findtext(".//title") or "").split())
    body = doc.find(".//body")
    text = (body if body is not None else doc).text_content()
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    return title, "\n".join(ln for ln in lines if ln)


def read_page(url: str, settings, max_chars: int = 8000, client: httpx.Client | None = None) -> dict[str, Any]:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not _is_public_host(p.hostname or "", settings):
        raise WebError("читать можно только внешние http(s)-страницы")
    c = client or httpx.Client(timeout=20, follow_redirects=False)
    try:
        current = url
        for _ in range(4):  # редиректы — вручную, чтобы проверить каждый адрес
            r = c.get(current, headers={"User-Agent": "Mozilla/5.0 (1C Project Copilot)"})
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                nxt = httpx.URL(current).join(r.headers["location"])
                if nxt.scheme not in ("http", "https") or not _is_public_host(nxt.host, settings):
                    raise WebError("страница перенаправляет во внутреннюю сеть — не читается")
                current = str(nxt)
                continue
            break
    except httpx.HTTPError as exc:
        raise WebError(f"страница не открылась: {type(exc).__name__}") from exc
    finally:
        if client is None:
            c.close()
    if r.status_code != 200:
        raise WebError(f"страница не открылась: HTTP {r.status_code}")
    ctype = r.headers.get("content-type", "")
    if not any(t in ctype for t in ("text/html", "text/plain", "application/xhtml")):
        raise WebError(f"не текстовая страница ({ctype or 'тип неизвестен'})")
    data = r.content[:PAGE_MAX_BYTES]
    title, text = html_text(data, r.encoding) if "html" in ctype else ("", data.decode(r.encoding or "utf-8", "replace"))
    journal(settings, {"read": current, "chars": len(text)})
    return {"url": current, "title": title, "text": text[:max_chars], "truncated": len(text) > max_chars}
