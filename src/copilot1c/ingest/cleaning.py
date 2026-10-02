"""Очистка текста писем: баннеры, подписи, дисклеймеры, ссылки Safe Links, персональные данные.

Разбор цепочки на отдельные письма — в thread.py; здесь обрабатывается текст одного письма.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

# Служебные баннеры почтовых шлюзов
_BANNER_RE = re.compile(
    r"^\s*(External mail\s*:.*|\[?EXTERNAL\]?.*|ВНЕШНЕЕ ПИСЬМО.*|Внимание! Письмо от внешнего отправителя.*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Начало подписи: всё ниже отрезается
_SIGNATURE_RE = re.compile(
    r"^\s*(--\s*$|С уважением\b|С наилучшими пожеланиями\b|Best regards\b|Kind regards\b|Regards,?\s*$|"
    r"Cordialement\b|Спасибо[.!,]?\s*$|Thanks[.!,]?\s*$|Thank you[.!,]?\s*$)",
    re.IGNORECASE | re.MULTILINE,
)
# Начало юридического дисклеймера: всё ниже отрезается
_DISCLAIMER_START_RE = re.compile(
    r"^\s*(Conformément à la Charte|In accordance with the .{0,60}Charter|Avis\s*:|Notice\s*:|Disclaimer\s*:|"
    r"Данное сообщение (и любые|является|содержит)|Это (сообщение|письмо) (и любые|может содержать|содержит)|"
    r"This (e-?mail|message) (and any|is confidential|may contain|contains))",
    re.IGNORECASE | re.MULTILINE,
)
# Абзацы-дисклеймеры в середине текста (если нет явного начала)
_DISCLAIMER_MARKERS = (
    ("конфиденциальн", "получател"),
    ("confidential", "recipient"),
    ("intended", "recipient"),
    ("this e-mail", "disclose"),
)

_SAFELINK_RE = re.compile(r"https?://[\w.-]*safelinks\.protection\.outlook\.com/\S+?(?=[\s>)\]]|$)", re.IGNORECASE)
_MAILTO_RE = re.compile(r"\s*<mailto:[^>]+>\s*")
_ANGLE_URL_RE = re.compile(r"\s*<(https?://[^>\s]+)>")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)(?:\s*(?:#|доб\.?)\s*\d+)?")


def unwrap_safelinks(text: str) -> str:
    def _unwrap(m: re.Match[str]) -> str:
        qs = parse_qs(urlparse(m.group(0)).query)
        return unquote(qs["url"][0]) if "url" in qs else m.group(0)

    return _SAFELINK_RE.sub(_unwrap, text)


def strip_banners(text: str) -> str:
    return _BANNER_RE.sub("", text)


def strip_signature(text: str) -> str:
    m = _SIGNATURE_RE.search(text)
    return text[: m.start()].rstrip() if m else text


def strip_disclaimers(text: str) -> str:
    m = _DISCLAIMER_START_RE.search(text)
    if m:
        text = text[: m.start()]
    paras = re.split(r"\n\s*\n", text)
    kept = [p for p in paras if not any(a in p.casefold() and b in p.casefold() for a, b in _DISCLAIMER_MARKERS)]
    return "\n\n".join(kept).strip()


def tidy_links(text: str) -> str:
    """Убирает дубли адресов «x@y <mailto:x@y>» и ссылки-картинки в угловых скобках."""
    text = _MAILTO_RE.sub(" ", text)
    return _ANGLE_URL_RE.sub("", text)


def mask_pii(text: str, keep_domains: tuple[str, ...] = ()) -> str:
    """Маскирует телефоны и адреса e-mail. Домен сохраняется — по нему видна организация."""

    def _mask_email(m: re.Match[str]) -> str:
        addr = m.group(0)
        domain = addr.rsplit("@", 1)[1].casefold()
        return addr if domain in keep_domains else f"<email@{domain}>"

    text = _EMAIL_RE.sub(_mask_email, text)
    return _PHONE_RE.sub("<телефон>", text)


def normalize_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ").replace("​", "")
    lines = [ln.rstrip() for ln in text.split("\n")]
    text = "\n".join("" if not ln.strip() else ln for ln in lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def dedent_quoted(text: str) -> str:
    """Цитаты Outlook бывают сдвинуты табуляцией; «>» — цитаты в стиле Gmail/Thunderbird."""
    lines = text.split("\n")
    lines = [re.sub(r"^(\t| {4}|>\s?)+", "", ln) for ln in lines]
    return "\n".join(lines)


def clean_email_text(text: str, mask: bool = True) -> str:
    """Очистка текста одного письма (без цитат — их отделяет thread.split_thread)."""
    text = normalize_whitespace(text)
    text = unwrap_safelinks(text)
    text = strip_banners(text)
    text = strip_signature(text)
    text = strip_disclaimers(text)
    text = tidy_links(text)
    if mask:
        text = mask_pii(text)
    return normalize_whitespace(text)


# Совместимость с первой версией API
def strip_quoted(text: str) -> str:
    from copilot1c.ingest.thread import split_thread

    return split_thread(text)[0].rstrip()


def clean_email_body(text: str) -> str:
    return clean_email_text(strip_quoted(text))
