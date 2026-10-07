"""Поиск в интернете для агента: что уходит наружу, разбор ответа Search API, чтение страниц, лимиты."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from copilot1c import web
from copilot1c.agent.tools import ToolContext, available_tools, make_handlers
from copilot1c.config import Settings

ERROR = "{Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: Поле объекта не обнаружено (КС_Гамма)"


def _settings(tmp_path=None, **kw):
    base = {"yc_api_key": "AQVN-key", "yc_folder_id": "b1g", "internal_domains": ("pierre-fabre.com",),
            "analysts": ("Иванов И.", "Петрова А."), "cache_dir": str(tmp_path or ".cache"), "onec_bin": "/nonexistent"}
    return Settings(**{**base, **kw})


def test_sanitize_keeps_error_objects_versions_and_removes_private(tmp_path):
    s = _settings(tmp_path)
    q = (f"Pierre Fabre: на pfmosvt1ceapp01 (10.1.2.3) после обновления до 11.5.27.75 на платформе 8.3.27.2342 "
         f"ошибка {ERROR}. Пишет m.smirnova@pierre-fabre.com, тел. +7 495 123-45-67, Иванов в курсе, "
         r"файл \\fs01\share\x.log")
    sent, removed = web.sanitize_query(q, s)
    for keep in ("11.5.27.75", "8.3.27.2342", "Поле объекта не обнаружено", "РеализацияТоваровУслуг", "КС_Гамма"):
        assert keep in sent, keep
    for gone in ("Pierre", "Fabre", "pfmosvt1ceapp01", "10.1.2.3", "smirnova", "pierre-fabre", "495", "Иванов", "fs01"):
        assert gone not in sent, gone
    assert any(r.startswith("сервер") for r in removed) and any(r.startswith("e-mail") for r in removed)
    with pytest.raises(web.WebError):
        web.sanitize_query("Pierre Fabre pfmosvt1ceapp01", s)  # после очистки отправлять нечего


XML = """<?xml version="1.0" encoding="utf-8"?>
<yandexsearch version="1.0"><response><found priority="all">2</found><results><grouping>
<group><doc><url>https://its.1c.ru/db/metod8dev/content/1</url><domain>its.1c.ru</domain>
<title>Ошибка <hlword>Поле объекта не обнаружено</hlword></title>
<passages><passage>Причина — реквизит удалён из <hlword>расширения</hlword>.</passage>
<passage>Перенесите реквизит.</passage></passages></doc></group>
<group><doc><url>https://infostart.ru/1c/articles/2</url><domain>infostart.ru</domain><title>Расширения УТ 11</title>
<headline>Как переносить доработки при обновлении</headline></doc></group>
</grouping></results></response></yandexsearch>"""


def test_parse_results_and_errors():
    res = web.parse_results(XML.encode(), 5)
    assert res[0] == {"title": "Ошибка Поле объекта не обнаружено", "url": "https://its.1c.ru/db/metod8dev/content/1",
                      "domain": "its.1c.ru", "snippet": "Причина — реквизит удалён из расширения. … Перенесите реквизит."}
    assert res[1]["snippet"] == "Как переносить доработки при обновлении" and len(web.parse_results(XML.encode(), 1)) == 1
    nothing = b'<yandexsearch><response><error code="15">Nothing found</error></response></yandexsearch>'
    assert web.parse_results(nothing, 5) == []
    with pytest.raises(web.WebError):
        web.parse_results(b'<yandexsearch><response><error code="32">Limit exceeded</error></response></yandexsearch>', 5)


def test_web_search_request_and_journal(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"rawData": base64.b64encode(XML.encode()).decode()})

    s = _settings(tmp_path)
    out = web.web_search(f"pfmosvt1ceapp01 {ERROR}", s, sites=["its.1c.ru", "bad site"],
                         client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["auth"] == "Api-Key AQVN-key" and seen["body"]["folderId"] == "b1g"
    q = seen["body"]["query"]["queryText"]
    assert "pfmosvt1ceapp01" not in q and q.endswith("(site:its.1c.ru)")  # мусор в sites отброшен
    assert len(out["results"]) == 2 and out["removed"]
    log = json.loads((Path(tmp_path) / "web" / "web_search.jsonl").read_text(encoding="utf-8"))
    assert "pfmosvt1ceapp01" in log["query"] and "pfmosvt1ceapp01" not in log["sent"]  # видно, что вырезано
    assert log["results"][0].startswith("https://its.1c.ru")

    bad = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403, text="forbidden")))
    with pytest.raises(web.WebError, match="HTTP 403"):
        web.web_search("Поле объекта не обнаружено", s, client=bad)
    with pytest.raises(web.WebError, match="нет ключа"):
        web.web_search("Поле объекта не обнаружено", _settings(tmp_path, yc_api_key=""))


def test_read_page_blocks_internal_and_extracts_text(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    for url in ("http://localhost:8100/health", "http://127.0.0.1/", "http://192.168.55.111/", "http://magic.lcl/",
                "https://portal.pierre-fabre.com/x", "file:///etc/passwd", "ftp://its.1c.ru/"):
        with pytest.raises(web.WebError):
            web.read_page(url, s)

    monkeypatch.setattr(web, "_is_public_host", lambda host, settings: host != "evil.example")
    html = ("<html><head><title>Расширения</title><script>alert(1)</script></head><body><nav>меню</nav>"
            "<p>Реквизит <b>КС_Гамма</b> нужно</p><p>перенести в расширение.</p></body></html>")

    def handler(request: httpx.Request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://evil.example/admin"})
        return httpx.Response(200, html=html)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    page = web.read_page("https://its.1c.ru/db/1", s, client=client)
    assert page["title"] == "Расширения" and "КС_Гамма" in page["text"] and "alert" not in page["text"]
    assert "меню" not in page["text"] and page["truncated"] is False
    with pytest.raises(web.WebError, match="внутреннюю сеть"):
        web.read_page("https://its.1c.ru/redirect", s, client=client)


def test_agent_tools_limits_and_sources(tmp_path, monkeypatch):
    s = _settings(tmp_path, web_max_searches=2, web_max_pages=1)
    ctx = ToolContext(s, Path("data/dumps"), None)
    names = {t["function"]["name"] for t in available_tools(ctx)}
    assert {"web_search", "read_page"} <= names
    off = ToolContext(_settings(tmp_path, web_search=False), Path("x"), None)
    assert "web_search" not in {t["function"]["name"] for t in available_tools(off)}

    monkeypatch.setattr(web, "web_search", lambda q, settings, sites=None: {
        "query_sent": q, "removed": [], "results": [{"title": "ИТС", "url": "https://its.1c.ru/1", "domain": "its.1c.ru",
                                                     "snippet": "…"}]})
    monkeypatch.setattr(web, "read_page", lambda url, settings: {"url": url, "title": "ИТС", "text": "текст",
                                                                  "truncated": False})
    h = make_handlers(ctx)
    r = h["web_search"]("Поле объекта не обнаружено")
    assert r["results"][0]["источник"] == "интернет: ИТС — https://its.1c.ru/1"
    h["web_search"]("ещё раз")
    assert "лимит" in h["web_search"]("третий")["error"]
    assert h["read_page"]("https://its.1c.ru/1")["text"] == "текст" and "лимит" in h["read_page"]("https://x.ru")["error"]
    assert ctx.web_sources == [{"title": "ИТС", "url": "https://its.1c.ru/1", "domain": "its.1c.ru", "read": True}]

    monkeypatch.setattr(web, "web_search", lambda *a, **kw: (_ for _ in ()).throw(web.WebError("пусто")))
    ctx2 = ToolContext(s, Path("x"), None)
    assert make_handlers(ctx2)["web_search"]("x")["error"] == "пусто"  # ошибка — агенту текстом


def test_settings_blocked_terms_from_env(monkeypatch):
    monkeypatch.setenv("COPILOT_WEB_BLOCKED_TERMS", "Pierre Fabre, Пьер Фабр, ПФ Рус")
    s = Settings(_env_file=None)
    assert s.web_blocked_terms == ("Pierre Fabre", "Пьер Фабр", "ПФ Рус")
    assert "ПФ Рус" in web.blocked_terms(SimpleNamespace(internal_domains=(), analysts=(),
                                                         web_blocked_terms=s.web_blocked_terms))
