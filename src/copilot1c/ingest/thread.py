"""Восстановление цепочки переписки из цитируемых хвостов.

В выгрузке почты часто есть только последнее письмо цепочки, а ответы, ради которых ведётся поиск
(«почему 11.5.27.75, а не 11.6», «подходит ли 8.3.27.2342»), живут только в цитатах. Поэтому
хвост не выбрасывается, а режется на отдельные письма по блокам заголовков From/Sent/To/Subject
(Outlook RU/EN) и строкам «… wrote:» / «… пишет:» (Gmail, Thunderbird, Яндекс).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, tzinfo

from dateutil import parser as dateparser

from copilot1c.ingest.cleaning import dedent_quoted

_FIELD_ALIASES = {
    "from": ("from", "от", "отправитель", "de"),
    "sent": ("sent", "отправлено", "date", "дата", "envoyé"),
    "to": ("to", "кому", "à"),
    "cc": ("cc", "копия", "сс"),
    "subject": ("subject", "тема", "objet"),
}
_FIELD_RE = re.compile(r"^[\t >]*(\w+)\s*:\s*(.*)$")
_SEPARATOR_RE = re.compile(r"^[\t >]*(-{3,}.*-{3,}|_{10,})\s*$")
_WROTE_RE = re.compile(
    r"^[\t >]*(?:On\s+(?P<d1>.+?),?\s+(?P<n1>[^,]+?)\s+wrote:|"
    r"(?P<d2>.+?\d{1,2}:\d{2}),?\s+(?P<n2>.+?)\s*(?:пишет|написал\(а\)|написал|написала)\s*:|"
    r"(?P<d3>.*?\d{4}.*?\d{1,2}:\d{2}),\s+(?P<n3>[^,]+?<[^>]+@[^>]+>)\s*:)\s*$",
    re.IGNORECASE,
)
_MONTHS_RU = {
    "янв": "January", "фев": "February", "мар": "March", "апр": "April", "май": "May", "мая": "May",
    "июн": "June", "июл": "July", "авг": "August", "сен": "September", "окт": "October", "ноя": "November",
    "дек": "December",
}
_MONTH_RU_RE = re.compile(r"\b(янв|фев|мар|апр|ма[йя]|июн|июл|авг|сен|окт|ноя|дек)[а-яё]*\.?", re.IGNORECASE)
_WEEKDAY_RE = re.compile(r"\b(понедельник|вторник|среда|четверг|пятница|суббота|воскресенье|пн|вт|ср|чт|пт|сб|вс)"
                         r"[,.]?\s*", re.IGNORECASE)
MAX_HEADER_SPAN = 14  # строк от From до последнего поля заголовка (с учётом пустых строк)


@dataclass
class QuotedMessage:
    sender: str
    sender_email: str
    date: datetime | None
    to: str
    cc: str
    subject: str
    text: str  # сырой текст письма без заголовка и без следующих цитат


def parse_date(value: str, tz: tzinfo | None = None) -> datetime | None:
    v = value.strip()
    if not v:
        return None
    v = _WEEKDAY_RE.sub("", v)
    v = _MONTH_RU_RE.sub(lambda m: _MONTHS_RU[m.group(1).lower()], v)
    v = re.sub(r"\s+(at|в)\s+", " ", v, flags=re.IGNORECASE).replace(" г.", " ").replace("г.", " ")
    try:
        dt = dateparser.parse(v, fuzzy=True, dayfirst=True)
    except (ValueError, OverflowError):
        return None
    if dt.tzinfo is None and tz is not None:
        dt = dt.replace(tzinfo=tz)
    return dt


def split_address(value: str) -> tuple[str, str]:
    """«SOKOLOV Dmitry [mailto:x@y]» / «Имя <x@y>» / «x@y» → (имя, адрес)."""
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", value)
    email = m.group(0).lower() if m else ""
    name = re.sub(r"[<\[(]\s*(mailto:)?[^>\])]*@[^>\])]*[>\])]", "", value)
    name = re.sub(r"<mailto:[^>]*>", "", name).strip(" \t\"'<>;,")
    if not name or "@" in name:
        name = email.split("@")[0] if email else name
    return name, email


def _field(line: str) -> tuple[str, str] | None:
    m = _FIELD_RE.match(line)
    if not m:
        return None
    key = m.group(1).casefold()
    for canon, aliases in _FIELD_ALIASES.items():
        if key in aliases:
            return canon, m.group(2).strip()
    return None


def _find_headers(lines: list[str]) -> list[tuple[int, int, dict[str, str]]]:
    """Блоки заголовков цитат: (первая строка, строка после блока, поля)."""
    found: list[tuple[int, int, dict[str, str]]] = []
    i = 0
    while i < len(lines):
        f = _field(lines[i])
        if f and f[0] == "from" and f[1]:
            fields = {"from": f[1]}
            j, last = i + 1, i
            while j < len(lines) and j - i <= MAX_HEADER_SPAN:
                if not lines[j].strip() or _SEPARATOR_RE.match(lines[j]):
                    j += 1
                    continue
                g = _field(lines[j])
                if not g or g[0] in fields:
                    break
                fields[g[0]] = g[1]
                last = j
                j += 1
            if "sent" in fields or "subject" in fields:
                start = i - 1 if i > 0 and _SEPARATOR_RE.match(lines[i - 1]) else i
                found.append((start, last + 1, fields))
                i = last + 1
                continue
        m = _WROTE_RE.match(lines[i])
        if m:
            date_s = m.group("d1") or m.group("d2") or m.group("d3") or ""
            name = m.group("n1") or m.group("n2") or m.group("n3") or ""
            found.append((i, i + 1, {"from": name, "sent": date_s}))
        i += 1
    return found


def split_thread(body: str, tz: tzinfo | None = None) -> tuple[str, list[QuotedMessage]]:
    """(текст самого письма, процитированные письма от новых к старым)."""
    lines = body.replace("\r\n", "\n").split("\n")
    headers = _find_headers(lines)
    if not headers:
        return body, []
    own = "\n".join(lines[: headers[0][0]])
    quoted: list[QuotedMessage] = []
    for k, (_start, end, fields) in enumerate(headers):
        stop = headers[k + 1][0] if k + 1 < len(headers) else len(lines)
        text = dedent_quoted("\n".join(lines[end:stop]))
        name, email = split_address(fields.get("from", ""))
        quoted.append(
            QuotedMessage(
                sender=name, sender_email=email, date=parse_date(fields.get("sent", ""), tz),
                to=fields.get("to", ""), cc=fields.get("cc", ""), subject=fields.get("subject", ""), text=text,
            )
        )
    return own, quoted
