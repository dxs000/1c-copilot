"""Структурный разбор таблиц, независимый от формата (docx, pdf, xlsx).

Таблица приводится к сетке: у каждой ячейки есть начальная колонка; строка, растянутая на всю
ширину, — заголовок группы. Затем таблица классифицируется:
  * тест-кейсы ПиМИ (№ · Функция · Методика · Критерий · Результат), шаги одного кейса —
    соседние строки с одинаковым №;
  * план тестирования ТЗ (№ · Наименование объекта · Процедура проверки);
  * покрытие «документ → пункты плана тестирования»;
  * всё остальное — общая таблица (строки «заголовок: значение»).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from copilot1c.ingest.document import (
    Coverage,
    GenericTable,
    PlanItem,
    TableRow,
    TestCase,
    TestStep,
    expand_ranges,
)
from copilot1c.ingest.entities import find_md_objects


@dataclass
class RawRow:
    cells: list[tuple[int, str]]  # (начальная колонка сетки, текст)
    full_width: bool = False

    def texts(self) -> list[str]:
        return [t for _, t in self.cells]


@dataclass
class RawTable:
    rows: list[RawRow]
    ncols: int
    extra: dict = field(default_factory=dict)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# Ключевые слова колонок: (поле, слова, обязательно с начала строки)
_FIELDS: dict[str, list[tuple[str, ...]]] = {
    "pimi": [("num", ("№", "n п/п", "номер", "#")), ("function", ("функция", "проверяемая функция")),
             ("method", ("методика", "порядок проверки", "шаги", "действия")),
             ("criterion", ("критерий", "ожидаемый результат")), ("result", ("результат", "статус", "отметка"))],
    "plan": [("num", ("№", "номер")), ("object", ("наименование объекта", "объект")),
             ("procedure", ("процедура проверки", "процедура", "методика"))],
    "coverage": [("num", ("№",)), ("document", ("документ",)), ("items", ("пункты плана", "пункты")),
                 ("coverage", ("покрытие",))],
}


def _map_header(header: list[tuple[int, str]], kind: str) -> dict[int, str]:
    """Колонка сетки → поле. Одно поле может занимать несколько колонок (объединённый заголовок)."""
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for col, text in header:
        low = _norm(text).casefold()
        if not low:
            continue
        for name, words in _FIELDS[kind]:
            if any(w in low for w in words) and (name not in used or name == "procedure"):
                # «Критерий» не должен перехватываться «результатом» и наоборот: порядок полей важен
                if name == "result" and ("критерий" in low or "ожидаем" in low):
                    continue
                mapping[col] = name
                used.add(name)
                break
    return mapping


def _classify(header: list[tuple[int, str]]) -> tuple[str, dict[int, str]] | None:
    joined = " ".join(_norm(t).casefold() for _, t in header)
    if ("методика" in joined or "шаги" in joined) and ("критерий" in joined or "ожидаем" in joined):
        return "pimi", _map_header(header, "pimi")
    if "процедура проверки" in joined and ("объект" in joined):
        return "plan", _map_header(header, "plan")
    if "пункты плана" in joined:
        return "coverage", _map_header(header, "coverage")
    return None


def _row_values(row: RawRow, mapping: dict[int, str]) -> dict[str, list[str]]:
    vals: dict[str, list[str]] = {}
    for col, text in row.cells:
        name = mapping.get(col)
        if name is None:  # ячейка начинается внутри объединённой колонки заголовка — ищем ближайшую слева
            left = [c for c in mapping if c <= col]
            name = mapping[max(left)] if left else None
        t = _norm(text) if name != "method" and name != "procedure" else text.strip()
        if name and t and t not in vals.get(name, []):
            vals.setdefault(name, []).append(t)
    return vals


def _first(vals: dict[str, list[str]], key: str) -> str:
    return vals.get(key, [""])[0]


def _num(text: str) -> str:
    return text.strip().rstrip(".").strip()


def _group_level(text: str) -> int:
    m = re.match(r"^\s*(\d+(?:\.\d+)*)\.?\s*\S", text)
    return m.group(1).count(".") + 1 if m else 0


class _GroupPath:
    """Иерархия строк-заголовков: «1.Обработки» → «1.1 EDI FML» → …"""

    def __init__(self, base: str):
        self.base = base
        self.stack: list[tuple[int, str]] = []

    def push(self, text: str) -> None:
        # Нумерованная строка задаёт уровень сама; длинная ненумерованная — пояснение к группе (уровень ниже)
        if _group_level(text):
            level = _group_level(text)
        elif len(_norm(text)) > 120 and self.stack:
            level = self.stack[-1][0] + 1 if _group_level(self.stack[-1][1]) or len(self.stack[-1][1]) <= 120 \
                else self.stack[-1][0]
        else:
            numbered = [lvl for lvl, t in self.stack if _group_level(t)]
            level = (max(numbered) + 1) if numbered else 1
        self.stack = [(lvl, t) for lvl, t in self.stack if lvl < level] + [(level, _norm(text))]

    def __str__(self) -> str:
        return " / ".join([p for p in [self.base] if p] + [t for _, t in self.stack])


def parse_table(table: RawTable, section: str, known_objects: Iterable[str] | None = None):
    """Возвращает (вид, записи): ("pimi", [TestCase]) | ("plan", [PlanItem]) | ("coverage", [Coverage]) |
    ("generic", GenericTable) | ("empty", None)."""
    rows = [r for r in table.rows if any(t.strip() for t in r.texts())]
    if not rows:
        return "empty", None
    header_row = rows[0]
    cls = _classify(header_row.cells)

    if cls is None:
        return "generic", _generic(rows, section, table.ncols)

    kind, mapping = cls
    group = _GroupPath(section)
    if kind == "coverage":
        out_cov: list[Coverage] = []
        for r in rows[1:]:
            v = _row_values(r, mapping)
            raw = _first(v, "items")
            if _first(v, "document") and raw:
                out_cov.append(Coverage(document=_first(v, "document"), items=expand_ranges(raw), items_raw=raw,
                                        coverage=_first(v, "coverage")))
        return "coverage", out_cov

    records: list = []
    for r in rows[1:]:
        if r.full_width or len({_norm(t) for t in r.texts() if t.strip()}) == 1 and not _num(
                _first(_row_values(r, mapping), "num")).isdigit():
            group.push(next(t for t in r.texts() if t.strip()))
            continue
        v = _row_values(r, mapping)
        num = _num(_first(v, "num"))
        prev = records[-1] if records else None
        same = prev is not None and (num == prev.num or not num)
        if kind == "pimi":
            step = TestStep(method="\n".join(v.get("method", [])), criterion=_first(v, "criterion"),
                            result=_first(v, "result"))
            if same:
                if step.method or step.criterion or step.result:
                    prev.steps.append(step)
            elif num:
                records.append(TestCase(num=num, section=str(group), function=_first(v, "function"), steps=[step]))
        else:  # plan
            procs = v.get("procedure", [])
            if same:
                prev.procedures += [p for p in procs if p not in prev.procedures]
            elif num:
                records.append(PlanItem(num=num, group=str(group), object=_first(v, "object"),
                                        procedures=list(procs)))
    for rec in records:
        rec.objects = find_md_objects(rec.text(), known_objects)
        if kind == "plan" and rec.object and not any(rec.object in o for o in rec.objects):
            rec.objects.append(f"«{rec.object}»")  # синоним объекта; разрешается по реестру метаданных
    return kind, records


def _generic(rows: list[RawRow], section: str, ncols: int) -> GenericTable:
    header_cells = rows[0].cells
    has_header = len(rows) > 1 and not rows[0].full_width and len(header_cells) > 1
    header = {col: _norm(t) for col, t in header_cells} if has_header else {}
    group = _GroupPath("")
    out: list[TableRow] = []
    for r in rows[1:] if has_header else rows:
        if r.full_width and ncols > 1:
            group.push(r.texts()[0])
            continue
        values: dict[str, str] = {}
        for i, (col, text) in enumerate(r.cells):
            key = header.get(col) or (f"Колонка {i + 1}" if header else str(i + 1))
            t = text.strip()
            if t and values.get(key) != t:
                values[key] = t if key not in values else f"{values[key]} | {t}"
        if values:
            out.append(TableRow(group=str(group), values=values))
    return GenericTable(section=section, header=list(header.values()), rows=out)
