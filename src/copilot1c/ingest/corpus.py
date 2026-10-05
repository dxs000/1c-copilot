"""Корпус проекта: все файлы и вложения, восстановленные цепочки писем, дедупликация между файлами.

Одно и то же письмо приходит самостоятельным файлом, вложением в другое письмо и цитатой в ответах;
один и тот же документ — файлом и вложениями в несколько писем. В индекс каждое попадает один раз,
а остальные места появления сохраняются как aliases/received — для ответа «когда и кем отправлено».
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from copilot1c.config import Settings, get_settings
from copilot1c.ingest.attachments import ImageText, Skipped, parse_bytes, parse_path
from copilot1c.ingest.chunking import document_chunks
from copilot1c.ingest.document import ParsedDocument
from copilot1c.ingest.msg import EmailMessage, ParsedEmail, message_chunk, walk
from copilot1c.ingest.ocr import MIN_IMAGE_BYTES, OcrUnavailable, ocr
from copilot1c.models import Chunk, DocType

SUPPORTED_GLOB = ("*.msg", "*.eml", "*.docx", "*.docm", "*.pdf", "*.xlsx", "*.xlsm", "*.doc", "*.xls", "*.rtf",
                  "*.odt", "*.zip", "*.txt", "*.png", "*.jpg", "*.jpeg")


def _same_document(a: ParsedDocument, b: ParsedDocument) -> bool:
    """Один документ в разных форматах (docx и подписанный PDF): тип, версия, дата и начало названия совпадают."""
    if a.doc_type != b.doc_type or a.version != b.version:
        return False
    if a.doc_date and b.doc_date and a.doc_date != b.doc_date:
        return False
    ta, tb = (re.sub(r"\W+", "", x.title.casefold()) for x in (a, b))
    n = min(len(ta), len(tb))
    return n >= 30 and ta[:n] == tb[:n]


def _richness(d: ParsedDocument) -> int:
    """docx богаче PDF: в нём есть комментарии, правки и точная структура."""
    return (0 if d.filename.lower().endswith(".pdf") else 10) + bool(d.comments) + bool(d.removed)


def _doc_fingerprint(d: ParsedDocument) -> str:
    parts = [d.title] + [p for s in d.sections for p in s.paragraphs] + [t.text() for t in d.test_cases] \
        + [p.text() for p in d.plan_items]
    norm = re.sub(r"\W+", "", "".join(parts).casefold())
    return hashlib.sha1(norm.encode()).hexdigest()


@dataclass
class Corpus:
    project: str = ""
    known_objects: list[str] = field(default_factory=list)
    settings: Settings | None = None
    ocr_images_in_docs: bool = True

    messages: list[EmailMessage] = field(default_factory=list)
    documents: list[ParsedDocument] = field(default_factory=list)
    images: list[ImageText] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    _msg_keys: dict[str, int] = field(default_factory=dict)
    _doc_keys: dict[str, int] = field(default_factory=dict)
    _img_keys: set[str] = field(default_factory=set)

    # --- загрузка ---

    def add_paths(self, paths: Iterable[str | Path]) -> Corpus:
        exclude = {d.casefold() for d in (self.settings or get_settings()).ingest_exclude_dirs}
        for p in paths:
            p = Path(p)
            files = sorted(f for f in p.rglob("*") if f.is_file() and not f.name.startswith(("~$", "."))
                           and not {x.casefold() for x in f.relative_to(p).parts[:-1]} & exclude) \
                if p.is_dir() else [p]
            for f in files:
                self._add_results(parse_path(f, self.known_objects, self.settings), context=None)
        return self

    def add_bytes(self, data: bytes, filename: str, source: str) -> Corpus:
        self._add_results(parse_bytes(data, filename, source, self.known_objects, self.settings), context=None)
        return self

    def _add_results(self, results, context: str | None) -> None:
        for r in results:
            if isinstance(r, ParsedEmail):
                self._add_email(r)
            elif isinstance(r, ParsedDocument):
                self._add_document(r, context)
            elif isinstance(r, ImageText):
                key = hashlib.sha1(r.text.encode()).hexdigest()
                if key not in self._img_keys:
                    self._img_keys.add(key)
                    self.images.append(r)
            elif isinstance(r, Skipped):
                self.skipped.append(r)

    def _add_email(self, e: ParsedEmail) -> None:
        for part in walk(e):
            for m in part.messages:
                self._add_message(m)
            own = part.message
            context = f"вложение письма «{own.subject}»" + (f" от {own.date:%d.%m.%Y}" if own.date else "") \
                + (f", {own.sender}" if own.sender else "")
            for att in part.attachments:
                if att.inline and Path(att.filename).suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".bmp"):
                    continue
                src = f"{part.source}#{att.filename}"
                self._add_results(parse_bytes(att.data, att.filename, src, self.known_objects, self.settings), context)

    def _add_message(self, m: EmailMessage) -> None:
        if not m.body:
            return
        keys = m.dedup_keys()
        hit = next((self._msg_keys[k] for k in keys if k in self._msg_keys), None)
        if hit is not None:
            existing = self.messages[hit]
            if existing.origin == "quoted" and m.origin == "file":  # версия из файла точнее цитаты
                self.messages[hit] = m
            for k in keys:
                self._msg_keys.setdefault(k, hit)
            return
        self.messages.append(m)
        for k in keys:
            self._msg_keys[k] = len(self.messages) - 1

    def _add_document(self, d: ParsedDocument, context: str | None) -> None:
        key = _doc_fingerprint(d)
        if key in self._doc_keys:
            existing = self.documents[self._doc_keys[key]]
            existing.aliases.append(d.source)
            if context and context not in existing.received:
                existing.received.append(context)
            return
        twin = next((i for i, x in enumerate(self.documents) if _same_document(x, d)), None)
        if twin is not None:
            existing = self.documents[twin]
            keep, drop = (d, existing) if _richness(d) > _richness(existing) else (existing, d)
            keep.aliases += [drop.source] + drop.aliases
            keep.received += [r for r in drop.received + ([context] if context else []) if r not in keep.received]
            self.documents[twin] = keep
            self._doc_keys[key] = twin
            return
        if context:
            d.received.append(context)
        if self.ocr_images_in_docs:
            for name, data in d.images:
                if len(data) < MIN_IMAGE_BYTES:
                    continue
                try:
                    text = ocr(data, self.settings or get_settings())
                except OcrUnavailable as exc:
                    self.skipped.append(Skipped(f"{d.source}#{name}", name, f"изображение в документе: {exc}"))
                    continue
                if text:
                    self.images.append(ImageText(f"{d.source}#{name}", name, text))
        self._doc_keys[key] = len(self.documents)
        self.documents.append(d)

    # --- выход ---

    def chunks(self) -> list[Chunk]:
        out: list[Chunk] = []
        for m in sorted(self.messages, key=lambda x: (x.date is None, x.date and x.date.timestamp() or 0)):
            out.append(message_chunk(m, self.project))
        for d in self.documents:
            out += document_chunks(d, self.project)
        for img in self.images:
            out.append(Chunk(text=f"Изображение {img.filename} (распознанный текст):\n{img.text}",
                             doc_type=DocType.DOC, source=img.source, title=f"Изображение {img.filename}",
                             project=self.project, extra={"origin": "ocr"}))
        return out

    def report(self) -> str:
        lines = [f"Писем: {len(self.messages)} (из файлов: {sum(m.origin == 'file' for m in self.messages)}, "
                 f"восстановлено из цитат: {sum(m.origin == 'quoted' for m in self.messages)})",
                 f"Документов: {len(self.documents)}"]
        for d in self.documents:
            lines.append(f"  • {d.doc_type.value:5} {d.title[:90]}" + (f" (ред. {d.version})" if d.version else ""))
            stats = [f"разделов {len(d.sections)}"]
            for label, items in (("тест-кейсов", d.test_cases), ("пунктов плана", d.plan_items),
                                 ("строк покрытия", d.coverage), ("комментариев", d.comments),
                                 ("удалённых формулировок", d.removed), ("таблиц", d.tables)):
                if items:
                    stats.append(f"{label} {len(items)}")
            lines.append("      " + ", ".join(stats))
            for r in d.received:
                lines.append(f"      получен: {r}")
            if d.aliases:
                lines.append(f"      дубли: {len(d.aliases)}")
            for w in d.warnings:
                lines.append(f"      ⚠ {w}")
        lines.append(f"Изображений распознано: {len(self.images)}")
        if self.skipped:
            lines.append(f"Пропущено: {len(self.skipped)}")
            for s in self.skipped:
                lines.append(f"  – {s.filename}: {s.reason}")
        return "\n".join(lines)
