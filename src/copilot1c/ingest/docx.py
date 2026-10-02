"""Разбор .docx в ParsedDocument.

Особенности реальных документов проекта, которые учитываются здесь:
  * заголовки часто не используют стили «Заголовок N»: уровень берётся из outlineLvl абзаца или
    его стиля (по цепочке наследования), а для автонумерованных заголовков — из уровня списка;
    номера автонумерации в тексте абзаца отсутствуют и восстанавливаются счётчиками;
  * оглавление (стили toc N) пропускается;
  * зачёркнутый текст — удалённые формулировки: в основной текст не идёт, сохраняется отдельно;
  * таблицы с объединёнными ячейками приводятся к сетке (gridSpan, vMerge);
  * комментарии рецензентов извлекаются вместе с фрагментом, к которому относятся.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from copilot1c.ingest.document import (
    Comment,
    ParsedDocument,
    Section,
    clean_filename,
    guess_doc_date,
    guess_doc_type,
    guess_doc_version,
)
from copilot1c.ingest.tables import RawRow, RawTable, parse_table

W_OUTLINE = qn("w:outlineLvl")
W_VAL = qn("w:val")
_HEADING_NAME_RE = re.compile(r"^(heading|заголовок)\s*(\d+)$", re.IGNORECASE)
_NUMBERED_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+\S")
MAX_HEADING_LEN = 200


def iter_blocks(doc: DocxDocument) -> Iterator[Paragraph | Table]:
    """Абзацы и таблицы в порядке следования (включая содержимое блоков w:sdt)."""

    def walk(parent):
        for child in parent.iterchildren():
            tag = child.tag
            if tag == qn("w:p"):
                yield Paragraph(child, doc)
            elif tag == qn("w:tbl"):
                yield Table(child, doc)
            elif tag == qn("w:sdt"):
                content = child.find(qn("w:sdtContent"))
                if content is not None:
                    yield from walk(content)

    yield from walk(doc.element.body)


# --- текст с учётом зачёркиваний и правок ---

def _run_is_struck(r) -> bool:
    rpr = r.find(qn("w:rPr"))
    if rpr is None:
        return False
    for tag in ("w:strike", "w:dstrike"):
        el = rpr.find(qn(tag))
        if el is not None and el.get(W_VAL) not in ("0", "false"):
            return True
    return False


def element_text(el) -> tuple[str, str]:
    """(текст, зачёркнутый текст) абзаца или ячейки. Удалённые правки (w:del) пропускаются."""
    kept: list[str] = []
    struck: list[str] = []
    for r in el.iter(qn("w:r")):
        if any(a.tag == qn("w:del") for a in r.iterancestors()):
            continue
        parts = []
        for c in r:
            if c.tag == qn("w:t"):
                parts.append(c.text or "")
            elif c.tag == qn("w:tab"):
                parts.append("\t")
            elif c.tag in (qn("w:br"), qn("w:cr")):
                parts.append("\n")
        text = "".join(parts)
        (struck if _run_is_struck(r) else kept).append(text)
    return "".join(kept), "".join(struck).strip()


def cell_text(tc) -> tuple[str, list[str]]:
    paras, removed = [], []
    for p in tc.iter(qn("w:p")):
        t, s = element_text(p)
        if t.strip():
            paras.append(t.strip())
        if s:
            removed.append(s)
    return "\n".join(paras), removed


# --- заголовки ---

class _StyleInfo:
    """Уровень структуры и признак списка по цепочке наследования стилей (с кэшем)."""

    def __init__(self, doc: DocxDocument):
        self.doc = doc
        self.cache: dict[str, tuple[int | None, bool, bool]] = {}

    def get(self, style) -> tuple[int | None, bool, bool]:
        """(outline level, стиль-заголовок, стиль оглавления)"""
        if style is None:
            return None, False, False
        key = style.style_id
        if key in self.cache:
            return self.cache[key]
        level, is_heading = None, False
        is_toc = (style.name or "").lower().startswith(("toc", "оглавление"))
        s = style
        while s is not None and level is None:
            m = _HEADING_NAME_RE.match(s.name or "")
            if m:
                level, is_heading = int(m.group(2)) - 1, True
                break
            ol = s.element.find(f"{qn('w:pPr')}/{W_OUTLINE}")
            if ol is not None and ol.get(W_VAL) is not None and int(ol.get(W_VAL)) < 9:
                level, is_heading = int(ol.get(W_VAL)), True
            s = s.base_style
        self.cache[key] = (level, is_heading, is_toc)
        return self.cache[key]


def _num_ilvl(p: Paragraph, style) -> int | None:
    ilvl = p._p.find(f"{qn('w:pPr')}/{qn('w:numPr')}/{qn('w:ilvl')}")
    if ilvl is None and style is not None:
        ilvl = style.element.find(f"{qn('w:pPr')}/{qn('w:numPr')}/{qn('w:ilvl')}")
    if ilvl is not None:
        return int(ilvl.get(W_VAL, "0"))
    has_num = p._p.find(f"{qn('w:pPr')}/{qn('w:numPr')}") is not None
    return 0 if has_num else None


def _is_bold(p: Paragraph) -> bool:
    runs = [r for r in p.runs if r.text.strip()]
    return bool(runs) and all(r.bold or (r.style is not None and r.style.font.bold) for r in runs)


def heading_level(p: Paragraph, text: str, styles: _StyleInfo) -> tuple[int | None, bool]:
    """(уровень заголовка 1..9 или None, автонумерация)"""
    if not text or len(text) > MAX_HEADING_LEN:
        return None, False
    style_level, style_heading, is_toc = styles.get(p.style)
    if is_toc:
        return None, False
    own = p._p.find(f"{qn('w:pPr')}/{W_OUTLINE}")
    level = int(own.get(W_VAL)) if own is not None and own.get(W_VAL) is not None else style_level
    if level is not None and level >= 9:
        level = None
    ilvl = _num_ilvl(p, p.style)
    auto_numbered = ilvl is not None and not _NUMBERED_RE.match(text)
    if level is not None:
        explicit = _NUMBERED_RE.match(text)
        if explicit and style_heading:
            level = explicit.group(1).count(".")  # явный номер «4.4 …» важнее уровня стиля
        elif style_heading and ilvl is not None:
            level = ilvl  # стиль «Заголовок 1» с многоуровневым списком: уровень задаёт список
        return level + 1, auto_numbered
    # Без стилей: короткий полужирный абзац с явным номером «3.2 …»
    m = _NUMBERED_RE.match(text)
    if m and _is_bold(p) and len(text) < 120 and not text.rstrip().endswith((";", ",")):
        return m.group(1).count(".") + 1, False
    return None, False


class _Numbering:
    def __init__(self):
        self.counters = [0] * 9

    def apply(self, level: int, text: str, auto: bool) -> str:
        m = _NUMBERED_RE.match(text)
        if m:  # явный номер синхронизирует счётчики: «4.4 Программа…»
            nums = [int(x) for x in m.group(1).split(".")]
            self.counters[: len(nums)] = nums
            self.counters[len(nums):] = [0] * (9 - len(nums))
            return text
        if not auto:
            return text
        self.counters[level - 1] += 1
        self.counters[level:] = [0] * (9 - level)
        nums = [str(c or 1) for c in self.counters[:level]]
        return f"{'.'.join(nums)} {text.strip()}"


# --- таблицы ---

def _raw_table(table: Table) -> tuple[RawTable, list[str]]:
    rows: list[RawRow] = []
    removed: list[str] = []
    above: dict[int, str] = {}  # текст по колонке сетки для vMerge=continue
    ncols = 0
    for tr in table._tbl.tr_lst:
        col = 0
        cells: list[tuple[int, str]] = []
        for tc in tr.tc_lst:
            span = tc.grid_span or 1
            vmerge = tc.tcPr.vMerge.val if tc.tcPr is not None and tc.tcPr.vMerge is not None else None
            if vmerge == "continue":
                text = above.get(col, "")
            else:
                text, rem = cell_text(tc)
                removed += rem
            cells.append((col, text))
            for c in range(col, col + span):
                above[c] = text
            col += span
        ncols = max(ncols, col)
        rows.append(RawRow(cells=cells))
    for r in rows:
        r.full_width = ncols > 1 and len(r.cells) == 1
    return RawTable(rows=rows, ncols=ncols), removed


# --- комментарии ---

def _comments(doc: DocxDocument) -> list[Comment]:
    part = next((p for p in doc.part.package.iter_parts() if str(p.partname) == "/word/comments.xml"), None)
    if part is None:
        return []
    from lxml import etree

    root = etree.fromstring(part.blob)
    meta = {}
    for c in root.iter(qn("w:comment")):
        text = "\n".join("".join(t.text or "" for t in p.iter(qn("w:t"))) for p in c.iter(qn("w:p"))).strip()
        meta[c.get(qn("w:id"))] = (c.get(qn("w:author"), ""), (c.get(qn("w:date")) or "")[:10], text)
    anchors: dict[str, list[str]] = {}
    open_ids: set[str] = set()
    for el in doc.element.body.iter():
        if el.tag == qn("w:commentRangeStart"):
            open_ids.add(el.get(qn("w:id")))
        elif el.tag == qn("w:commentRangeEnd"):
            open_ids.discard(el.get(qn("w:id")))
        elif el.tag in (qn("w:p"), qn("w:tc")) and open_ids:
            for cid in open_ids:
                anchors.setdefault(cid, []).append(" ")
        elif el.tag == qn("w:t") and open_ids:
            for cid in open_ids:
                anchors.setdefault(cid, []).append(el.text or "")
    return [
        Comment(author=a, date=d, anchor=re.sub(r"\s+", " ", "".join(anchors.get(cid, []))).strip()[:500], text=t)
        for cid, (a, d, t) in meta.items() if t
    ]


def _images(doc: DocxDocument) -> list[tuple[str, bytes]]:
    out = []
    for rel in doc.part.rels.values():
        if "image" in rel.reltype and not rel.is_external:
            out.append((Path(str(rel.target_part.partname)).name, rel.target_part.blob))
    return out


# --- сборка документа ---

def _title(blocks: list[tuple[str, bool]], fallback: str) -> str:
    """Первые полужирные абзацы до оглавления/первого заголовка."""
    parts: list[str] = []
    for text, bold in blocks:
        if text.casefold() in ("содержание", "оглавление"):
            break
        if bold:
            parts.append(text)
            if len(parts) == 3 or sum(map(len, parts)) > 200:
                break
        elif parts:
            break
    return " ".join(parts)[:300] or fallback


def parse_docx(path: str | Path, known_objects: Iterable[str] | None = None, source: str | None = None,
               filename: str | None = None) -> ParsedDocument:
    path = Path(path)
    filename = clean_filename(filename or path.name)
    doc = Document(str(path))
    styles = _StyleInfo(doc)
    numbering = _Numbering()

    sections: list[Section] = [Section(path=[])]
    stack: list[tuple[int, str]] = []
    removed: list[str] = []
    title_blocks: list[tuple[str, bool]] = []
    pending_tables: list[tuple[str, RawTable]] = []
    seen_heading = False

    for block in iter_blocks(doc):
        if isinstance(block, Paragraph):
            text, struck = element_text(block._p)
            text = text.strip()
            if struck:
                removed.append(struck)
            if not text:
                continue
            level, auto = heading_level(block, text, styles)
            if not seen_heading and level is None and len(title_blocks) < 12:
                title_blocks.append((text, _is_bold(block)))
            if level:
                seen_heading = True
                text = numbering.apply(level, text, auto)
                stack = [(lv, t) for lv, t in stack if lv < level] + [(level, text)]
                sections.append(Section(path=[t for _, t in stack], level=level))
            elif not styles.get(block.style)[2]:  # не оглавление
                sections[-1].paragraphs.append(text)
        else:
            raw, rem = _raw_table(block)
            removed += rem
            pending_tables.append((sections[-1].title, raw))
            sections[-1].paragraphs.append(f"[таблица {len(pending_tables)}]")

    title = _title(title_blocks, Path(filename).stem)
    head_text = " ".join(t for t, _ in title_blocks)
    parsed = ParsedDocument(
        source=source or str(path),
        filename=filename,
        title=title,
        doc_type=guess_doc_type(title, filename),
        version=guess_doc_version(head_text, filename),
        doc_date=guess_doc_date(filename, head_text),
        removed=[r for r in dict.fromkeys(removed) if len(re.findall(r"[А-Яа-яЁёA-Za-z]", r)) >= 8],
        comments=_comments(doc),
        images=_images(doc),
    )
    # Ссылки-заглушки «[таблица N]» заменяются: структурные таблицы уходят в реестры, общие — в tables
    for i, (section_title, raw) in enumerate(pending_tables, 1):
        kind, records = parse_table(raw, section_title, known_objects)
        if kind == "pimi":
            parsed.test_cases += records
        elif kind == "plan":
            parsed.plan_items += records
        elif kind == "coverage":
            parsed.coverage += records
        elif kind == "generic" and records.rows:
            parsed.tables.append(records)
        for s in sections:
            s.paragraphs = [p for p in s.paragraphs if p != f"[таблица {i}]"]
    parsed.sections = [s for s in sections if s.paragraphs or s.level]
    return parsed
