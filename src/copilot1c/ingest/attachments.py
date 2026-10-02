"""Разбор файлов любого поддерживаемого типа — и самостоятельных, и вложений писем.

Тип определяется по расширению и сигнатуре. Старые форматы Office (.doc, .xls, .rtf, .odt, .ppt…)
конвертируются LibreOffice в современные. Неподдерживаемое не теряется молча: попадает в
skipped с причиной и видно в отчёте.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from copilot1c.config import Settings, get_settings
from copilot1c.ingest.document import ParsedDocument, clean_filename, guess_doc_date, guess_doc_type, guess_doc_version
from copilot1c.ingest.ocr import MIN_IMAGE_BYTES, ocr
from copilot1c.ingest.tables import RawRow, RawTable, parse_table

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
TEXT_EXT = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".bsl", ".sql"}
CONVERT = {".doc": "docx", ".rtf": "docx", ".odt": "docx", ".xls": "xlsx", ".ods": "xlsx",
           ".ppt": "pdf", ".pptx": "pdf", ".odp": "pdf"}
MAX_ZIP_DEPTH = 3
MAX_FILE_BYTES = 200 * 1024 * 1024


@dataclass
class ImageText:
    source: str
    filename: str
    text: str


@dataclass
class Skipped:
    source: str
    filename: str
    reason: str


def kind_of(filename: str, data: bytes) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".msg":
        return "msg"
    if ext == ".eml":
        return "eml"
    if ext in (".docx", ".docm", ".dotx"):
        return "docx"
    if ext in (".xlsx", ".xlsm"):
        return "xlsx"
    if ext == ".pdf" or data[:4] == b"%PDF":
        return "pdf"
    if ext == ".zip" or (data[:2] == b"PK" and ext not in (".docx", ".xlsx")):
        return "zip"
    if ext in IMAGE_EXT:
        return "image"
    if ext in TEXT_EXT:
        return "text"
    if ext in CONVERT:
        return "convert"
    return "unknown"


def convert_with_soffice(data: bytes, filename: str, target: str, settings: Settings | None = None) -> bytes | None:
    s = settings or get_settings()
    if not shutil.which(s.soffice_bin):
        return None
    with tempfile.TemporaryDirectory() as d:
        src = Path(d, "in" + Path(filename).suffix.lower())
        src.write_bytes(data)
        subprocess.run([s.soffice_bin, "--headless", "--convert-to", target, "--outdir", d, str(src)],
                       capture_output=True, timeout=300)
        out = Path(d, f"in.{target}")
        return out.read_bytes() if out.exists() else None


def parse_xlsx(data: bytes, filename: str, source: str, known_objects=None) -> ParsedDocument:
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    doc = ParsedDocument(source=source, filename=clean_filename(filename), title=Path(clean_filename(filename)).stem,
                         doc_type=guess_doc_type(filename), version=guess_doc_version(filename),
                         doc_date=guess_doc_date(filename))
    for ws in wb.worksheets:
        merged = {}
        for rng in ws.merged_cells.ranges:
            for row in range(rng.min_row, rng.max_row + 1):
                for col in range(rng.min_col, rng.max_col + 1):
                    merged[(row, col)] = (rng.min_row, rng.min_col, rng.max_col - rng.min_col + 1)
        rows: list[RawRow] = []
        ncols = ws.max_column or 0
        for r in ws.iter_rows(min_row=1, max_row=ws.max_row):
            cells: list[tuple[int, str]] = []
            full = False
            for c in r:
                key = (c.row, c.column)
                if key in merged:
                    top, left, span = merged[key]
                    if c.column != left:
                        continue  # продолжение горизонтального объединения
                    value = ws.cell(top, left).value
                    full = full or (span >= ncols > 1)
                else:
                    value = c.value
                if value is not None and str(value).strip():
                    cells.append((c.column - 1, str(value).strip()))
            if cells:
                rows.append(RawRow(cells=cells, full_width=full or (ncols > 1 and len(cells) == 1 and cells[0][0] == 0
                                                                      and len(rows) > 0)))
        if not rows:
            continue
        kind, records = parse_table(RawTable(rows=rows, ncols=ncols), f"Лист «{ws.title}»", known_objects)
        if kind == "pimi":
            doc.test_cases += records
        elif kind == "plan":
            doc.plan_items += records
        elif kind == "coverage":
            doc.coverage += records
        elif kind == "generic" and records.rows:
            doc.tables.append(records)
    return doc


def parse_bytes(data: bytes, filename: str, source: str, known_objects=None, settings: Settings | None = None,
                depth: int = 0):
    """Разбирает файл и возвращает список результатов: ParsedDocument | ParsedEmail | ImageText | Skipped."""
    from copilot1c.ingest.docx import parse_docx
    from copilot1c.ingest.msg import parse_eml, parse_msg
    from copilot1c.ingest.pdf import parse_pdf

    s = settings or get_settings()
    if len(data) > MAX_FILE_BYTES:
        return [Skipped(source, filename, f"файл больше {MAX_FILE_BYTES // 2**20} МБ")]
    kind = kind_of(filename, data)
    suffix = Path(filename).suffix.lower() or ".bin"

    def via_tmp(fn, **kw):
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(data)
            name = f.name
        try:
            return fn(name, **kw)
        finally:
            Path(name).unlink(missing_ok=True)

    try:
        if kind == "msg":
            return [via_tmp(parse_msg, source=source)]
        if kind == "eml":
            return [parse_eml(data=data, source=source)]
        if kind == "docx":
            return [via_tmp(parse_docx, known_objects=known_objects, source=source, filename=filename)]
        if kind == "pdf":
            return [via_tmp(parse_pdf, known_objects=known_objects, source=source, filename=filename, settings=s)]
        if kind == "xlsx":
            return [parse_xlsx(data, filename, source, known_objects)]
        if kind == "image":
            if len(data) < MIN_IMAGE_BYTES:
                return [Skipped(source, filename, "маленькое изображение (логотип/иконка)")]
            text = ocr(data, s)
            if text is None:
                return [Skipped(source, filename, "изображение: OCR недоступен (нужен Yandex Vision или tesseract rus)")]
            return [ImageText(source, filename, text)] if text.strip() else []
        if kind == "text":
            text = data.decode("utf-8", errors="replace")
            doc = ParsedDocument(source=source, filename=filename, title=filename, doc_type=guess_doc_type(filename))
            from copilot1c.ingest.document import Section

            doc.sections = [Section(path=[], paragraphs=[p for p in text.split("\n\n") if p.strip()])]
            return [doc]
        if kind == "convert":
            target = CONVERT[suffix]
            converted = convert_with_soffice(data, filename, target, s)
            if converted is None:
                return [Skipped(source, filename, f"нужен LibreOffice для конвертации {suffix} → .{target}")]
            new_name = str(Path(filename).with_suffix(f".{target}"))
            return parse_bytes(converted, new_name, source, known_objects, s, depth)
        if kind == "zip":
            if depth >= MAX_ZIP_DEPTH:
                return [Skipped(source, filename, "слишком глубокая вложенность архивов")]
            out = []
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename
                    if not info.flag_bits & 0x800:  # имя не в UTF-8 — архивы из Windows пишут cp866
                        try:
                            name = name.encode("cp437").decode("cp866")
                        except UnicodeError:
                            pass
                    out += parse_bytes(z.read(info), Path(name).name, f"{source}#{name}", known_objects, s, depth + 1)
            return out
    except Exception as exc:  # один битый файл не должен останавливать загрузку корпуса
        return [Skipped(source, filename, f"ошибка разбора: {type(exc).__name__}: {exc}")]
    return [Skipped(source, filename, f"неподдерживаемый тип {suffix}")]


def parse_path(path: str | Path, known_objects=None, settings: Settings | None = None):
    path = Path(path)
    return parse_bytes(path.read_bytes(), clean_filename(path.name), str(path), known_objects, settings)
