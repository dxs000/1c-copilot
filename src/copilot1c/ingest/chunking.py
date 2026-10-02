"""Нарезка ParsedDocument на чанки индекса — по структуре документа, а не «по N токенов»."""

from __future__ import annotations

from copilot1c.ingest.document import ParsedDocument
from copilot1c.models import Chunk, DocType

MAX_CHUNK_CHARS = 2000


def _pack(lines: list[str], limit: int = MAX_CHUNK_CHARS) -> list[list[str]]:
    """Группы строк не длиннее limit; слишком длинная строка режется по предложениям."""
    groups: list[list[str]] = [[]]
    size = 0
    for line in lines:
        pieces = [line]
        if len(line) > limit:
            pieces, cur = [], ""
            for sent in line.replace(". ", ".\u0000").split("\u0000"):
                if len(cur) + len(sent) > limit and cur:
                    pieces.append(cur)
                    cur = ""
                cur += sent + " "
            pieces.append(cur.strip())
        for piece in pieces:
            if size + len(piece) > limit and groups[-1]:
                groups.append([])
                size = 0
            groups[-1].append(piece)
            size += len(piece) + 1
    return [g for g in groups if g]


def document_chunks(doc: ParsedDocument, project: str = "") -> list[Chunk]:
    base = {
        "source": doc.source,
        "project": project,
        "doc_version": doc.version,
    }
    extra_common = {"doc_title": doc.title[:200], "filename": doc.filename}
    if doc.doc_date:
        extra_common["doc_date"] = doc.doc_date.isoformat()
    if doc.received:
        extra_common["received"] = "; ".join(doc.received)[:500]
    head = f"{doc.title}" + (f" (ред. {doc.version})" if doc.version else "")

    def make(text: str, title: str, doc_type: DocType | None = None, objects=None, **extra) -> Chunk:
        body = text.split("\n", 1)[1] if text.startswith(head) and "\n" in text else text
        return Chunk(text=text, doc_type=doc_type or doc.doc_type, title=title, objects=list(objects or []),
                     extra={**extra_common, **{k: str(v) for k, v in extra.items() if v}}, body=body, **base)

    chunks: list[Chunk] = []
    from copilot1c.ingest.entities import find_md_objects

    for sec in doc.sections:
        if not sec.paragraphs:
            continue
        for i, group in enumerate(_pack(sec.paragraphs)):
            body = "\n".join(group)
            title = sec.title + (f" (часть {i + 1})" if i else "")
            chunks.append(make(f"{head}\n{title}\n\n{body}", title, objects=find_md_objects(body),
                               section=sec.number))

    for tc in doc.test_cases:
        chunks.append(make(f"{head}\n{tc.text()}", f"Тест-кейс № {tc.num}", DocType.PIMI, tc.objects,
                           test_case=tc.num, result=tc.result))

    for item in doc.plan_items:
        chunks.append(make(f"{head}\n{item.text()}", f"Пункт плана тестирования № {item.num}", objects=item.objects,
                           plan_item=item.num))

    for cov in doc.coverage:
        text = (f"{head}\nДокумент {cov.document} покрывает пункты плана тестирования: {cov.items_raw}."
                + (f" Покрытие: {cov.coverage}." if cov.coverage else ""))
        chunks.append(make(text, f"Покрытие: {cov.document[:80]}", coverage_doc=cov.document[:200]))

    for c in doc.comments:
        text = f"{head}\nКомментарий {c.author} от {c.date}" + (f" к фрагменту «{c.anchor}»" if c.anchor else "") \
               + f":\n{c.text}"
        chunks.append(make(text, f"Комментарий {c.author}", comment_author=c.author))

    if doc.removed:
        for i, group in enumerate(_pack(doc.removed)):
            chunks.append(make(f"{head}\nУдалённые (зачёркнутые) формулировки:\n" + "\n".join(f"– {r}" for r in group),
                               "Удалённые формулировки" + (f" (часть {i + 1})" if i else ""), revision="removed"))

    for t in doc.tables:
        lines = []
        for row in t.rows:
            prefix = f"[{row.group}] " if row.group else ""
            lines.append(prefix + "; ".join(f"{k}: {v}" for k, v in row.values.items()))
        for i, group in enumerate(_pack(lines)):
            title = f"{t.section} — таблица" + (f" (часть {i + 1})" if i else "")
            body = "\n".join(group)
            chunks.append(make(f"{head}\n{title}\n\n{body}", title, objects=find_md_objects(body)))

    return chunks
