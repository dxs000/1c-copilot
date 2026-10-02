"""Очистка текста писем перед индексацией.

В индекс идёт только новая часть письма: цитируемые хвосты переписки, дисклеймеры и
подписи отрезаются, ссылки Safe Links раскрываются, телефоны и e-mail маскируются.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

# Начало цитируемой переписки (Outlook RU/EN, Gmail, классические ">")
_QUOTE_HEADERS = [
    r"^-{2,}\s*(Original Message|Исходное сообщение|Пересылаемое сообщение|Forwarded message)\s*-{2,}",
    r"^(From|От|Отправлено|Sent):\s.+",
    r"^On .+ wrote:\s*$",
    r"^.{0,80}\d{1,2}[./]\d{1,2}[./]\d{2,4}.{0,40}(пишет|написал\(а\)|wrote):\s*$",
    r"^_{10,}\s*$",
]
_QUOTE_RE = re.compile("|".join(f"(?:{p})" for p in _QUOTE_HEADERS), re.IGNORECASE | re.MULTILINE)

# Абзацы-дисклеймеры: признаки конфиденциальности + обращение к «неверному получателю»
_DISCLAIMER_MARKERS = (
    ("конфиденциальн", "получател"),
    ("confidential", "recipient"),
    ("intended", "recipient"),
    ("this e-mail", "disclose"),
)

_SAFELINK_RE = re.compile(r"https?://[\w.-]*safelinks\.protection\.outlook\.com/\S+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
_SIGNATURE_RE = re.compile(r"^(--\s*|С уважением,?|Best regards,?|Kind regards,?)\s*$", re.IGNORECASE | re.MULTILINE)


def strip_quoted(text: str) -> str:
    """Оставить только новую часть письма — всё до первого заголовка цитаты."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    m = _QUOTE_RE.search(text)
    return text[: m.start()].rstrip() if m else text.rstrip()


def strip_signature(text: str) -> str:
    m = _SIGNATURE_RE.search(text)
    return text[: m.start()].rstrip() if m else text


def strip_disclaimers(text: str) -> str:
    paras = re.split(r"\n\s*\n", text)
    kept = []
    for p in paras:
        low = p.casefold()
        if any(a in low and b in low for a, b in _DISCLAIMER_MARKERS):
            continue
        kept.append(p)
    return "\n\n".join(kept).strip()


def unwrap_safelinks(text: str) -> str:
    def _unwrap(m: re.Match[str]) -> str:
        qs = parse_qs(urlparse(m.group(0)).query)
        return unquote(qs["url"][0]) if "url" in qs else m.group(0)

    return _SAFELINK_RE.sub(_unwrap, text)


def mask_pii(text: str, keep_domains: tuple[str, ...] = ()) -> str:
    """Маскирует телефоны и адреса e-mail. Домены из keep_domains оставляет (роль, а не человек)."""

    def _mask_email(m: re.Match[str]) -> str:
        addr = m.group(0)
        domain = addr.rsplit("@", 1)[1].casefold()
        return addr if domain in keep_domains else f"<email@{domain}>"

    text = _EMAIL_RE.sub(_mask_email, text)
    return _PHONE_RE.sub("<телефон>", text)


def clean_email_body(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = unwrap_safelinks(text)
    text = strip_quoted(text)
    text = strip_signature(text)
    text = strip_disclaimers(text)
    text = mask_pii(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
