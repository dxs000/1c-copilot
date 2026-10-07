"""Разбор писем .msg (Outlook) и .eml (RFC 822): письмо, процитированные письма, вложения рекурсивно."""

from __future__ import annotations

import email
import email.policy
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import extract_msg
from extract_msg.msg_classes import MessageBase

from copilot1c.ingest.cleaning import clean_email_text, mask_pii
from copilot1c.ingest.thread import split_address, split_thread
from copilot1c.models import Chunk, DocType


@dataclass
class Attachment:
    filename: str
    data: bytes
    inline: bool = False  # встроено в тело письма (картинка по cid)


@dataclass
class EmailMessage:
    """Одно письмо цепочки: из свойств MAPI/заголовков (origin=file) или восстановленное из цитаты."""

    subject: str
    sender: str
    sender_email: str
    to: str
    cc: str
    date: datetime | None
    body: str  # очищенный текст
    source: str
    origin: str = "file"  # file | quoted
    attachments: list[str] = field(default_factory=list)  # имена вложений (для заголовка чанка)
    message_id: str | None = None  # Message-ID — только у писем-файлов, у цитат его нет

    @property
    def thread_id(self) -> str:
        return thread_id(self.subject)

    def dedup_keys(self) -> list[str]:
        """Одно и то же письмо приходит файлом, вложением и цитатой — ключи для склейки."""
        who = _person_key(self.sender, self.sender_email)
        keys = []
        if self.date:
            keys.append(f"{who}|{self.date:%Y-%m-%d %H:%M}")
        body = re.sub(r"\W+", "", self.body.casefold())[:120]
        if len(body) >= 20:
            keys.append(f"{who}|{body}")
        return keys


@dataclass
class ParsedEmail:
    messages: list[EmailMessage]  # [0] — само письмо, дальше — цитаты от новых к старым
    attachments: list[Attachment] = field(default_factory=list)
    nested: list[ParsedEmail] = field(default_factory=list)
    source: str = ""

    @property
    def message(self) -> EmailMessage:
        return self.messages[0]


def _person_key(name: str, addr: str) -> str:
    """Фамилия (первое слово имени) — она одинакова в свойствах письма и в цитате, где адреса может не быть."""
    first = re.split(r"[\s,]+", name.strip())[0] if name.strip() else ""
    return first.casefold() or addr.split("@")[0].casefold()


def thread_id(subject: str) -> str:
    s = subject or ""
    while True:
        new = re.sub(r"^\s*(re|fw|fwd|ответ|пересл|rv|tr|aw|wg)\s*(\[\d+\])?\s*:\s*", "", s, flags=re.IGNORECASE)
        if new == s:
            return s.strip()
        s = new


def _build(subject: str, sender_raw: str, to: str, cc: str, date: datetime | None, raw_body: str, source: str,
           attachment_names: list[str], message_id: str | None = None) -> list[EmailMessage]:
    tz = date.tzinfo if date else None
    own, quoted = split_thread(raw_body, tz)
    name, addr = split_address(sender_raw)
    msgs = [EmailMessage(subject=subject, sender=name, sender_email=addr, to=to, cc=cc, date=date,
                         body=clean_email_text(own), source=source, attachments=attachment_names,
                         message_id=(message_id or "").strip().replace("\x00", "") or None)]
    for i, q in enumerate(quoted, 1):
        msgs.append(EmailMessage(
            subject=q.subject or subject, sender=q.sender, sender_email=q.sender_email, to=q.to, cc=q.cc,
            date=q.date, body=clean_email_text(q.text), source=f"{source}#quote{i}", origin="quoted",
        ))
    return msgs


# --- .msg ---

def _parse_mapi(msg: MessageBase, source: str) -> ParsedEmail:
    attachments: list[Attachment] = []
    nested: list[ParsedEmail] = []
    for i, att in enumerate(msg.attachments):
        data = att.data
        if isinstance(data, MessageBase):
            nested.append(_parse_mapi(data, f"{source}#att{i}"))
        elif isinstance(data, bytes):
            name = att.longFilename or att.shortFilename or att.displayName or f"attachment_{i}"
            # cid есть и у обычных вложений Outlook; встроенные в тело картинки помечены как скрытые
            inline = bool(getattr(att, "hidden", False))
            attachments.append(Attachment(filename=name, data=data, inline=inline))
    date = msg.date if isinstance(msg.date, datetime) else None
    names = [a.filename for a in attachments if not a.inline] + [n.message.subject or "письмо" for n in nested]
    messages = _build(msg.subject or "", msg.sender or "", msg.to or "", msg.cc or "", date, msg.body or "",
                      source, names, getattr(msg, "messageId", None))
    return ParsedEmail(messages=messages, attachments=attachments, nested=nested, source=source)


def parse_msg(path: str | Path, source: str | None = None) -> ParsedEmail:
    msg = extract_msg.openMsg(str(path))
    try:
        return _parse_mapi(msg, source or str(path))
    finally:
        msg.close()


# --- .eml ---

def _eml_body(m: email.message.EmailMessage) -> str:
    part = m.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        import html

        text = html.unescape(text)
    return text


def _parse_eml_message(m: email.message.EmailMessage, source: str) -> ParsedEmail:
    attachments: list[Attachment] = []
    nested: list[ParsedEmail] = []
    for i, part in enumerate(m.iter_attachments()):
        if part.get_content_type() == "message/rfc822":
            inner = part.get_payload()[0] if part.is_multipart() else part.get_content()
            nested.append(_parse_eml_message(inner, f"{source}#att{i}"))
            continue
        data = part.get_payload(decode=True) or b""
        name = part.get_filename() or f"attachment_{i}"
        inline = part.get_content_maintype() == "image" and (
            (part.get("Content-Disposition") or "").lower().startswith("inline") or bool(part.get("Content-ID")))
        attachments.append(Attachment(filename=name, data=data, inline=inline))
    try:
        date = email.utils.parsedate_to_datetime(m["Date"]) if m["Date"] else None
    except (TypeError, ValueError):
        date = None
    names = [a.filename for a in attachments if not a.inline] + [n.message.subject for n in nested]
    messages = _build(str(m["Subject"] or ""), str(m["From"] or ""), str(m["To"] or ""), str(m["Cc"] or ""),
                      date, _eml_body(m), source, names, str(m["Message-ID"] or ""))
    return ParsedEmail(messages=messages, attachments=attachments, nested=nested, source=source)


def parse_eml(path: str | Path | None = None, data: bytes | None = None, source: str | None = None) -> ParsedEmail:
    raw = data if data is not None else Path(path).read_bytes()
    m = email.message_from_bytes(raw, policy=email.policy.default)
    return _parse_eml_message(m, source or str(path))


# --- обход и чанки ---

def walk(e: ParsedEmail) -> Iterator[ParsedEmail]:
    yield e
    for n in e.nested:
        yield from walk(n)


def all_messages(e: ParsedEmail) -> Iterator[EmailMessage]:
    for p in walk(e):
        yield from p.messages


def people(value: str) -> str:
    """Список адресатов → «Имя (домен); …»: имена нужны для поиска, сами адреса — нет."""
    out = []
    for part in re.split(r";|,(?![^<\[]*[>\]])", re.sub(r"<mailto:[^>]*>", "", value or "")):
        name, addr = split_address(part)
        if name or addr:
            domain = addr.split("@")[1] if "@" in addr else ""
            out.append(f"{name} ({domain})" if domain and name else name or domain)
    return "; ".join(dict.fromkeys(out))


def message_chunk(m: EmailMessage, project: str = "") -> Chunk:
    domain = m.sender_email.split("@")[1] if "@" in m.sender_email else ""
    sender = f"{m.sender} ({domain})" if domain else m.sender
    header = [f"Тема: {m.subject}", f"От: {sender}"]
    if m.to:
        header.append(f"Кому: {people(m.to)}")
    if m.cc:
        header.append(f"Копия: {people(m.cc)}")
    if m.date:
        header.append(f"Дата: {m.date:%d.%m.%Y %H:%M}")
    if m.attachments:
        header.append("Вложения: " + ", ".join(m.attachments))
    return Chunk(
        body=f"{m.subject}\n{m.body}",
        text=mask_pii("\n".join(header)) + "\n\n" + m.body,
        doc_type=DocType.EMAIL,
        source=m.source,
        title=m.subject,
        project=project,
        date=m.date,
        author=m.sender,
        extra={"thread_id": m.thread_id, "origin": m.origin},
    )


def email_chunks(e: ParsedEmail, project: str = "") -> list[Chunk]:
    """Чанки писем одного файла с дедупликацией внутри него (для склейки между файлами — corpus.py)."""
    seen: set[str] = set()
    out: list[Chunk] = []
    for m in sorted(all_messages(e), key=lambda x: x.origin != "file"):  # письма из файлов приоритетнее цитат
        keys = m.dedup_keys()
        if not m.body or any(k in seen for k in keys):
            continue
        seen.update(keys)
        out.append(message_chunk(m, project))
    return out
