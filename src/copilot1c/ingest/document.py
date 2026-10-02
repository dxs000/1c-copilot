"""Общая модель разобранного документа — одна для .docx, .pdf и прочих форматов."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from copilot1c.models import DocType


@dataclass
class Section:
    path: list[str]  # иерархия заголовков с номерами: ["4 Программы испытаний", "4.1 …"]
    paragraphs: list[str] = field(default_factory=list)
    level: int = 0

    @property
    def title(self) -> str:
        return " / ".join(self.path) or "(начало документа)"

    @property
    def number(self) -> str:
        m = re.match(r"^\s*(\d+(?:\.\d+)*)", self.path[-1]) if self.path else None
        return m.group(1) if m else ""


@dataclass
class TestStep:
    method: str = ""
    criterion: str = ""
    result: str = ""


@dataclass
class TestCase:
    """Тест-кейс ПиМИ: № · Функция · шаги (Методика → Критерий → Результат)."""

    num: str
    section: str
    function: str = ""
    steps: list[TestStep] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)

    @property
    def result(self) -> str:
        results = [s.result for s in self.steps if s.result]
        bad = [r for r in results if re.search(r"не\s+работ|ошибк|не\s+пройд|fail", r, re.IGNORECASE)]
        if bad:
            return bad[0]
        return results[0] if results and all(r == results[0] for r in results) else "; ".join(dict.fromkeys(results))

    def text(self) -> str:
        parts = [f"Тест-кейс № {self.num}. Раздел: {self.section}"]
        if self.function:
            parts.append(f"Функция: {self.function}")
        for i, s in enumerate(self.steps, 1):
            prefix = f"Шаг {i}. " if len(self.steps) > 1 else ""
            if s.method:
                parts.append(f"{prefix}Методика проверки: {s.method}")
            if s.criterion:
                parts.append(f"{' ' * len(prefix)}Критерий успешности: {s.criterion}")
            if s.result:
                parts.append(f"{' ' * len(prefix)}Результат: {s.result}")
        return "\n".join(parts)


@dataclass
class PlanItem:
    """Пункт плана тестирования из ТЗ: № · объект · процедуры проверки."""

    num: str
    group: str
    object: str
    procedures: list[str] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)

    def text(self) -> str:
        lines = [f"Пункт плана тестирования № {self.num}. {self.group}", f"Объект проверки: {self.object}"]
        lines += [f"Процедура проверки: {p}" for p in self.procedures]
        return "\n".join(lines)


@dataclass
class Coverage:
    """Строка таблицы «документ → пункты плана тестирования»."""

    document: str
    items: list[str]
    items_raw: str
    coverage: str = ""


@dataclass
class Comment:
    author: str
    date: str
    anchor: str  # фрагмент текста, к которому оставлен комментарий
    text: str


@dataclass
class TableRow:
    group: str  # заголовок группы из объединённой строки таблицы
    values: dict[str, str]


@dataclass
class GenericTable:
    section: str
    header: list[str]
    rows: list[TableRow]


@dataclass
class ParsedDocument:
    source: str
    filename: str
    title: str
    doc_type: DocType
    version: str | None = None
    doc_date: date | None = None
    sections: list[Section] = field(default_factory=list)
    test_cases: list[TestCase] = field(default_factory=list)
    plan_items: list[PlanItem] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    comments: list[Comment] = field(default_factory=list)
    tables: list[GenericTable] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)  # зачёркнутые (удалённые) формулировки
    images: list[tuple[str, bytes]] = field(default_factory=list)  # для OCR/VLM
    warnings: list[str] = field(default_factory=list)
    received: list[str] = field(default_factory=list)  # «вложение письма «…» от дд.мм.гггг, отправитель»
    aliases: list[str] = field(default_factory=list)  # другие источники того же документа (дубли)

    def full_text(self) -> str:
        return "\n".join(p for s in self.sections for p in s.paragraphs)


# --- распознавание типа, версии и даты документа ---

_TYPE_PATTERNS = [
    (DocType.PIMI, r"программ\w*\s+и\s+методик\w*\s+испытан|(^|[^а-яё])п(и)?ми([^а-яё]|$)"),
    (DocType.DS, r"дополнительн\w*\s+соглашени|(^|[^а-яё])дс\s*№?\s*\d"),
    (DocType.TZ, r"техническ\w*\s+задани|(^|[^а-яё])тз([^а-яё]|$)"),
]


def guess_doc_type(*texts: str) -> DocType:
    """Тип по заголовку документа, затем по имени файла (первое совпадение по порядку текстов)."""
    for text in texts:
        low = (text or "").casefold().replace("_", " ")
        for doc_type, pattern in _TYPE_PATTERNS:
            if re.search(pattern, low):
                return doc_type
    return DocType.DOC


def guess_doc_version(*texts: str) -> str | None:
    for text in texts:
        m = re.search(r"(?:верси[яи]\s+документа|ред\.?|редакци[яи]|ver\.?|версия)\s*(\d+(?:\.\d+)?)", text or "",
                      re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def guess_doc_date(*texts: str) -> date | None:
    for text in texts:
        m = re.search(r"(?<!\d)(\d{2})[.\-_]?(\d{2})[.\-_]?(20\d{2})(?!\d)", text or "")
        if m:
            try:
                return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            except ValueError:
                continue
    return None


def clean_filename(name: str) -> str:
    """Убирает префикс загрузки вида «29fed8ad-»."""
    return re.sub(r"^[0-9a-f]{8}-", "", name)


def expand_ranges(text: str) -> list[str]:
    """«1–9, 129, 147–151» → ["1", …, "9", "129", "147", …, "151"]."""
    out: list[str] = []
    for part in re.split(r"[,;]\s*", text):
        m = re.match(r"^\s*(\d+)\s*[–—-]\s*(\d+)\s*$", part)
        if m and int(m.group(2)) >= int(m.group(1)) and int(m.group(2)) - int(m.group(1)) < 500:
            out += [str(i) for i in range(int(m.group(1)), int(m.group(2)) + 1)]
        elif re.match(r"^\s*\d+\s*$", part):
            out.append(part.strip())
    return out
