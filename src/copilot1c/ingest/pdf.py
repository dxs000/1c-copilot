"""Разбор PDF в ParsedDocument: текст вне таблиц, таблицы (с переносом через страницы), сканы через OCR."""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from pathlib import Path

import pdfplumber

from copilot1c.config import Settings, get_settings
from copilot1c.ingest.document import (
    ParsedDocument,
    Section,
    clean_filename,
    guess_doc_date,
    guess_doc_type,
    guess_doc_version,
)
from copilot1c.ingest.ocr import ocr
from copilot1c.ingest.tables import RawRow, RawTable, _classify, parse_table

_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,3})\.?\s+([А-ЯЁA-Z«\"].{2,100})$")
_APPENDIX_RE = re.compile(r"^\s*(Приложение\s*№?\s*\d+.*)$", re.IGNORECASE)
_TOC_LINE_RE = re.compile(r"(\.{4,}|…{2,})\s*\d+\s*$")
_TITLE_KEYS_RE = re.compile(r"техническое\s+задание|дополнительное\s+соглашение|программа\s+и\s+методика|"
                            r"протокол|договор\b|акт\b|спецификаци", re.IGNORECASE)
_PAGE_NUM_RE = re.compile(r"^\s*(стр\.?|страница|page)?\s*\d+\s*(из\s*\d+)?\s*$", re.IGNORECASE)
MIN_TEXT_CHARS = 40  # меньше на странице с картинками — считаем сканом


def _to_raw(rows: list[list[str | None]]) -> RawTable:
    ncols = max((len(r) for r in rows), default=0)
    out: list[RawRow] = []
    for r in rows:
        cells = [(i, (c or "").strip()) for i, c in enumerate(r) if c is not None]
        full = ncols > 1 and len(cells) == 1 and cells[0][0] == 0
        out.append(RawRow(cells=cells, full_width=full))
    return RawTable(rows=out, ncols=ncols)


def _heading(line: str, next_line: str = "") -> tuple[int, str] | None:
    """Нумерованная короткая строка, за которой не следует продолжение со строчной буквы."""
    if line.rstrip().endswith((";", ",", ":", ".")) or len(line) > 100 or len(line.split()) > 14:
        return None
    if next_line[:1].islower():
        return None
    m = _HEADING_RE.match(line)
    if m and not re.search(r"\d{2}\.\d{2}\.\d{4}", line):
        return m.group(1).count(".") + 1, line.strip()
    m = _APPENDIX_RE.match(line)
    if m and len(line) < 80:
        return 1, line.strip()
    return None


def parse_pdf(path: str | Path, known_objects: Iterable[str] | None = None, source: str | None = None,
              filename: str | None = None, settings: Settings | None = None) -> ParsedDocument:
    s = settings or get_settings()
    path = Path(path)
    filename = clean_filename(filename or path.name)
    sections: list[Section] = [Section(path=[])]
    stack: list[tuple[int, str]] = []
    tables: list[tuple[str, RawTable]] = []
    first_lines: list[str] = []
    warnings: list[str] = []
    ocr_pages = 0

    with pdfplumber.open(str(path)) as pdf:
        meta_title = (pdf.metadata or {}).get("Title") or ""
        for page_no, page in enumerate(pdf.pages, 1):
            found = page.find_tables()
            bboxes = [t.bbox for t in found]

            def outside(obj, bboxes=bboxes):
                x, top = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
                return not any(b[0] <= x <= b[2] and b[1] <= top <= b[3] for b in bboxes)

            text = page.filter(outside).extract_text() or ""
            if len(text.strip()) < MIN_TEXT_CHARS and not found and page.images:
                img = page.to_image(resolution=200).original
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                recognized = ocr(buf.getvalue(), s)
                if recognized is None:
                    warnings.append(f"стр. {page_no}: скан без текстового слоя, OCR недоступен")
                    continue
                text, ocr_pages = recognized, ocr_pages + 1

            lines = [ln.strip() for ln in text.splitlines()]
            lines = [ln for ln in lines if ln and not _PAGE_NUM_RE.match(ln) and not _TOC_LINE_RE.search(ln)]
            for i, line in enumerate(lines):
                if len(first_lines) < 15:
                    first_lines.append(line)
                h = _heading(line, lines[i + 1] if i + 1 < len(lines) else "")
                if h:
                    level, title = h
                    stack = [(lv, t) for lv, t in stack if lv < level] + [(level, title)]
                    sections.append(Section(path=[t for _, t in stack], level=level))
                else:
                    sections[-1].paragraphs.append(line)

            for t in found:
                raw = _to_raw(t.extract())
                prev = tables[-1][1] if tables else None
                # Продолжение таблицы с прошлой страницы: таблица в начале страницы, без своей шапки,
                # ширина совпадает (±1 колонка из-за объединённых ячеек)
                continues = (
                    prev is not None and t.bbox[1] < page.height * 0.15 and abs(prev.ncols - raw.ncols) <= 1
                    and raw.rows and (raw.rows[0].texts() == prev.rows[0].texts()
                                      or _classify(raw.rows[0].cells) is None)
                )
                if continues:
                    rows = raw.rows
                    if rows and prev.rows and rows[0].texts() == prev.rows[0].texts():
                        rows = rows[1:]  # повтор шапки
                    prev.rows += rows
                else:
                    tables.append((sections[-1].title, raw))

    # Абзацы PDF приходят строками — склеиваем переносы внутри абзаца
    for sec in sections:
        sec.paragraphs = _join_lines(sec.paragraphs)

    key = next((i for i, ln in enumerate(first_lines) if _TITLE_KEYS_RE.search(ln)), 0)
    title = meta_title if len(meta_title) > 8 else " ".join(first_lines[key:key + 2])[:300]
    head = " ".join(first_lines)
    parsed = ParsedDocument(
        source=source or str(path), filename=filename, title=title or Path(filename).stem,
        doc_type=guess_doc_type(head, filename), version=guess_doc_version(head, filename),
        doc_date=guess_doc_date(filename, head), warnings=warnings,
    )
    if ocr_pages:
        parsed.warnings.append(f"распознано OCR страниц: {ocr_pages}")
    for section_title, raw in tables:
        kind, records = parse_table(raw, section_title, known_objects)
        if kind == "pimi":
            parsed.test_cases += records
        elif kind == "plan":
            parsed.plan_items += records
        elif kind == "coverage":
            parsed.coverage += records
        elif kind == "generic" and records.rows:
            parsed.tables.append(records)
    parsed.sections = [sec for sec in sections if sec.paragraphs or sec.level]
    return parsed


def _join_lines(lines: list[str]) -> list[str]:
    """Строка, не заканчивающаяся точкой/двоеточием, продолжается следующей строкой со строчной буквы."""
    out: list[str] = []
    for line in lines:
        if out and not out[-1].endswith((".", ":", ";", "!", "?")) and line[:1].islower():
            out[-1] = (out[-1][:-1] if out[-1].endswith("-") else out[-1] + " ") + line
        else:
            out.append(line)
    return out
