"""Файлы, приложенные к вопросу в чате («+»): текст для агента и, для письма, черновик обращения.

Приложенное не попадает в базу проекта: это контекст одного вопроса. Чтобы сохранить материал
навсегда — вкладка «Материалы»; чтобы завести обращение по письму — карточка в ответе чата.

Что извлекается (те же разборщики, что при индексации, без сохранения):
- письма .msg/.eml — вся цепочка (письмо, цитаты, письма-вложения): тема, автор, дата, текст без подписей
  и дисклеймеров; файлы внутри письма разбираются как отдельные вложения;
- документы (.docx, .pdf, .xlsx, старые форматы через LibreOffice), текст и логи — текст разделов;
- картинки и сканы — распознанный текст (OCR), если он доступен.
Объём ограничен: на файл и на все вложения вместе (модель не должна утонуть в приложенном).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from copilot1c.email_intake import is_email_file

log = logging.getLogger("copilot1c.chat_files")
PER_FILE_CHARS = 12000
TOTAL_CHARS = 30000
MAX_FILES = 10


@dataclass
class Attached:
    filename: str
    kind: str                    # email | document | image | skipped
    text: str = ""
    note: str = ""               # почему пусто или что обрезано
    inner: list[str] = field(default_factory=list)  # файлы внутри письма
    subject: str = ""            # тема верхнего письма без RE/FW — для поиска по базе

    def out(self) -> dict[str, Any]:
        return {"filename": self.filename, "kind": self.kind, "chars": len(self.text), "note": self.note,
                "inner": self.inner}


def _email_text(name: str, data: bytes) -> tuple[str, list[tuple[str, bytes]], str]:
    from copilot1c.email_intake import chronological, read_chain
    from copilot1c.ingest.cleaning import clean_email_text
    from copilot1c.ingest.msg import thread_id

    chain, files = read_chain(name, data)
    subject = thread_id(chain[0].subject) if chain else ""
    parts = []
    for m in chronological(chain):
        who = f"{m.name} <{m.email}>" if m.email else m.name
        when = f"{m.date:%d.%m.%Y %H:%M}" if m.date else "дата неизвестна"
        body = clean_email_text(m.raw)
        if body:
            parts.append(f"Письмо «{m.subject}» — {who}, {when}:\n{body}")
    return "\n\n".join(parts), files, subject


def _document_text(name: str, data: bytes, settings) -> tuple[str, str]:
    from copilot1c.ingest.attachments import ImageText, Skipped, parse_bytes
    from copilot1c.ingest.document import ParsedDocument

    texts, notes = [], []
    for item in parse_bytes(data, name, name, settings=settings):
        if isinstance(item, ParsedDocument):
            body = [item.full_text()]
            body += [tc.text() for tc in item.test_cases]  # ПиМИ: тест-кейсы лежат отдельно от разделов
            body += [p.text() for p in item.plan_items]
            for t in item.tables:
                body += [" | ".join(r.values.values()) for r in t.rows[:200]]
            texts.append("\n".join(b for b in body if b))
        elif isinstance(item, ImageText):
            texts.append(item.text)
        elif isinstance(item, Skipped):
            notes.append(item.reason)
    return "\n\n".join(t for t in texts if t.strip()), "; ".join(notes)


def _known_letters_view(name: str, data: bytes, conn, settings) -> str | None:
    """Письмо глазами агента с учётом базы: сводка известной ветки, новые письма целиком, известные — строкой.
    None — сверить не удалось (тогда — вся цепочка, как раньше)."""
    from copilot1c.ingest.attachments import parse_bytes
    from copilot1c.ingest.msg import ParsedEmail
    from copilot1c.letters import LetterStore, agent_view

    try:
        st = LetterStore(conn, getattr(settings, "project", ""))
        res = [r for p in parse_bytes(data, name, name, settings=settings) if isinstance(p, ParsedEmail)
               for r in st.check(p)]
        conn.rollback()
    except Exception as exc:  # noqa: BLE001 — база недоступна: письмо целиком
        conn.rollback()
        log.warning("письмо %s не сверено с ветками: %s: %s", name, type(exc).__name__, exc)
        return None
    if not res or all(len(r.new_letters) == len(r.letters) and not r.summary_text for r in res):
        return None  # ничего не известно — полный текст цепочки нагляднее
    return agent_view(res)


def extract(files: list[tuple[str, bytes]], settings=None, conn=None) -> list[Attached]:
    """Текст каждого файла (и файлов внутри писем) с ограничением объёма. conn — подключение к базе: письма
    сверяются с известными ветками, и агент получает сводку ветки и только новые письма, а не весь хвост."""
    import hashlib

    out: list[Attached] = []
    queue = [(n, d, None) for n, d in files[:MAX_FILES]]
    top = {hashlib.sha256(d).hexdigest(): n for n, d in files[:MAX_FILES]}
    while queue:
        name, data, parent = queue.pop(0)
        shown = f"{name} (из письма «{parent}»)" if parent else name
        same = top.get(hashlib.sha256(data).hexdigest()) if parent else None
        if same:  # вложение письма приложено и отдельно — второй раз не разбираем и не тратим лимит
            out.append(Attached(shown, "skipped", note=f"то же, что приложенный файл «{same}»"))
            continue
        try:
            if is_email_file(name):
                text, inner, subject = _email_text(name, data)
                view = _known_letters_view(name, data, conn, settings) if conn is not None else None
                a = Attached(shown, "email", view or text, inner=[n for n, _ in inner], subject=subject,
                             note="известные письма ветки — сводкой" if view else "")
                queue[0:0] = [(n, d, name) for n, d in inner]  # вложения письма — сразу за ним
            else:
                text, note = _document_text(name, data, settings)
                kind = "image" if name.lower().rsplit(".", 1)[-1] in ("png", "jpg", "jpeg", "gif", "bmp", "tif",
                                                                       "tiff") else "document"
                a = Attached(shown, kind if text else "skipped", text, note or ("" if text else "текст не найден"))
        except Exception as exc:  # noqa: BLE001 — один битый файл не мешает остальным
            log.warning("файл %s не разобран: %s: %s", name, type(exc).__name__, exc)
            a = Attached(shown, "skipped", note=f"не разобран: {type(exc).__name__}")
        out.append(a)

    used = 0
    for a in out:
        if len(a.text) > PER_FILE_CHARS:
            a.text, a.note = a.text[:PER_FILE_CHARS], (a.note + "; " if a.note else "") + "обрезан до начала файла"
        room = max(TOTAL_CHARS - used, 0)
        if len(a.text) > room:
            a.text = a.text[:room]
            a.note = (a.note + "; " if a.note else "") + ("не вошёл в лимит" if not room else "обрезан общим лимитом")
        used += len(a.text)
    return out


def context_block(items: list[Attached]) -> str:
    """Блок для сообщения агенту."""
    parts = [f"### Файл «{a.filename}»\n{a.text}" for a in items if a.text]
    return "\n\n".join(parts)
