"""Разбор писем Outlook .msg: свойства MAPI, тело и вложения рекурсивно (msg в msg)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import extract_msg
from extract_msg.msg_classes import MessageBase

from copilot1c.ingest.cleaning import clean_email_body
from copilot1c.models import Chunk, DocType


@dataclass
class Attachment:
    filename: str
    data: bytes


@dataclass
class ParsedEmail:
    subject: str
    sender: str
    to: str
    cc: str
    date: datetime | None
    body: str  # очищенная новая часть
    thread_id: str
    source: str
    attachments: list[Attachment] = field(default_factory=list)
    nested: list[ParsedEmail] = field(default_factory=list)


def _thread_id(subject: str) -> str:
    s = subject or ""
    while True:
        low = s.lower().lstrip()
        for p in ("re:", "fw:", "fwd:", "ответ:", "пересл:", "rv:"):
            if low.startswith(p):
                s = s.lstrip()[len(p):]
                break
        else:
            return s.strip()


def _parse_message(msg: MessageBase, source: str) -> ParsedEmail:
    parsed = ParsedEmail(
        subject=msg.subject or "",
        sender=msg.sender or "",
        to=msg.to or "",
        cc=msg.cc or "",
        date=msg.date if isinstance(msg.date, datetime) else None,
        body=clean_email_body(msg.body or ""),
        thread_id=_thread_id(msg.subject or ""),
        source=source,
    )
    for i, att in enumerate(msg.attachments):
        data = att.data
        if isinstance(data, MessageBase):
            parsed.nested.append(_parse_message(data, f"{source}#att{i}"))
        elif isinstance(data, bytes):
            name = att.longFilename or att.shortFilename or f"attachment_{i}"
            parsed.attachments.append(Attachment(filename=name, data=data))
    return parsed


def parse_msg(path: str | Path) -> ParsedEmail:
    path = Path(path)
    msg = extract_msg.openMsg(str(path))
    try:
        return _parse_message(msg, str(path))
    finally:
        msg.close()


def walk(email: ParsedEmail) -> Iterator[ParsedEmail]:
    yield email
    for n in email.nested:
        yield from walk(n)


def email_chunks(email: ParsedEmail, project: str = "") -> list[Chunk]:
    """Один чанк на письмо (новая часть), включая вложенные письма.

    Одинаковые тела (одно и то же письмо, вложенное в несколько цепочек) дедуплицируются.
    """
    seen: set[str] = set()
    chunks: list[Chunk] = []
    for e in walk(email):
        if not e.body or e.body in seen:
            continue
        seen.add(e.body)
        header = f"Тема: {e.subject}\nОт: {e.sender}\nКому: {e.to}"
        if e.date:
            header += f"\nДата: {e.date:%d.%m.%Y %H:%M}"
        if e.attachments:
            header += "\nВложения: " + ", ".join(a.filename for a in e.attachments)
        chunks.append(
            Chunk(
                text=f"{header}\n\n{e.body}",
                doc_type=DocType.EMAIL,
                source=e.source,
                title=e.subject,
                project=project,
                date=e.date,
                author=e.sender,
                extra={"thread_id": e.thread_id},
            )
        )
    return chunks
