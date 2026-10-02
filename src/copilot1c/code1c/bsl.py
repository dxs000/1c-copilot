"""Структурный разбор модулей BSL: метод = чанк индекса.

Лёгкий построчный парсер без внешних зависимостей. Для диагностик и точных ссылок
подключается BSL Language Server (см. docs/architecture.md); этого парсера достаточно
для чанкинга, графа вызовов первого приближения и поиска перехватов расширений.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_START_RE = re.compile(
    r"^\s*(?:(Асинх|Async)\s+)?(Процедура|Функция|Procedure|Function)\s+([\wЁё]+)\s*\((.*?)\)\s*"
    r"(Экспорт|Export)?",
    re.IGNORECASE,
)
_END_RE = re.compile(r"^\s*(КонецПроцедуры|КонецФункции|EndProcedure|EndFunction)\b", re.IGNORECASE)
_ANNOTATION_RE = re.compile(r"^\s*&([\wЁё]+)(?:\(\s*\"?([^\")]*)\"?\s*\))?")
_REGION_RE = re.compile(r"^\s*#(Область|Region)\s+([\wЁё]+)", re.IGNORECASE)
_END_REGION_RE = re.compile(r"^\s*#(КонецОбласти|EndRegion)", re.IGNORECASE)
_CALL_RE = re.compile(r"(?<![\wЁё.&])((?:[\wЁё]+\.)?[\wЁё]+)\s*\(")
_MD_REF_RE = re.compile(
    r"\b(Справочник|Документ|РегистрСведений|РегистрНакопления|РегистрБухгалтерии|Перечисление|"
    r"ПланВидовХарактеристик|Обработка|Отчет|ОбщийМодуль|Catalog|Document|InformationRegister|"
    r"AccumulationRegister|Enum)\.([\wЁё]+)",
)
_STRING_RE = re.compile(r'"(?:[^"]|"")*"')
_COMMENT_RE = re.compile(r"//.*$")

_KEYWORDS = {
    "если", "иначеесли", "пока", "для", "возврат", "новый", "тип", "типзнч", "знач", "не", "и", "или",
    "if", "elsif", "while", "for", "return", "new", "type", "typeof", "val", "not", "and", "or",
}

# Аннотации расширений: перехват типового метода
INTERCEPT_ANNOTATIONS = {"Перед", "После", "Вместо", "ИзменениеИКонтроль", "Before", "After", "Around",
                         "ChangeAndValidate"}
CONTEXT_ANNOTATIONS = {"НаСервере", "НаКлиенте", "НаСервереБезКонтекста", "НаКлиентеНаСервереБезКонтекста",
                       "НаКлиентеНаСервере", "AtServer", "AtClient", "AtServerNoContext"}


@dataclass
class Method:
    name: str
    kind: str  # Процедура | Функция
    params: str
    export: bool
    module: str  # путь модуля относительно корня выгрузки
    start_line: int  # 1-based, включая комментарий и аннотации
    end_line: int
    text: str
    comment: str = ""
    context: list[str] = field(default_factory=list)
    intercepts: list[tuple[str, str]] = field(default_factory=list)  # (аннотация, имя типового метода)
    region: str = ""
    calls: list[str] = field(default_factory=list)
    md_refs: list[str] = field(default_factory=list)

    @property
    def signature(self) -> str:
        sig = f"{self.kind} {self.name}({self.params})"
        return f"{sig} Экспорт" if self.export else sig


def _code_only(line: str) -> str:
    return _COMMENT_RE.sub("", _STRING_RE.sub('""', line))


def parse_module(text: str, module: str = "") -> list[Method]:
    lines = text.replace("\r\n", "\n").lstrip("﻿").split("\n")
    methods: list[Method] = []
    regions: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if m := _REGION_RE.match(line):
            regions.append(m.group(2))
        elif _END_REGION_RE.match(line) and regions:
            regions.pop()
        start = _START_RE.match(line)
        if not start:
            i += 1
            continue

        # Подняться вверх по аннотациям и комментарию над методом
        j = i - 1
        annotations: list[tuple[str, str]] = []
        while j >= 0 and (a := _ANNOTATION_RE.match(lines[j])):
            annotations.insert(0, (a.group(1), a.group(2) or ""))
            j -= 1
        comment_lines: list[str] = []
        while j >= 0 and lines[j].lstrip().startswith("//"):
            comment_lines.insert(0, lines[j].strip().lstrip("/").strip())
            j -= 1
        first = j + 1

        k = i + 1
        while k < len(lines) and not _END_RE.match(lines[k]):
            k += 1
        body = lines[i : k + 1]

        code = "\n".join(_code_only(ln) for ln in body[1:-1])
        calls = sorted({c for c in _CALL_RE.findall(code) if c.casefold() not in _KEYWORDS})
        md_refs = sorted({f"{a}.{b}" for a, b in _MD_REF_RE.findall("\n".join(body))})

        methods.append(
            Method(
                name=start.group(3),
                kind=start.group(2).capitalize(),
                params=start.group(4).strip(),
                export=bool(start.group(5)),
                module=module,
                start_line=first + 1,
                end_line=min(k, len(lines) - 1) + 1,
                text="\n".join(lines[first : k + 1]),
                comment="\n".join(comment_lines),
                context=[a for a, _ in annotations if a in CONTEXT_ANNOTATIONS],
                intercepts=[(a, arg) for a, arg in annotations if a in INTERCEPT_ANNOTATIONS],
                region=regions[-1] if regions else "",
                calls=calls,
                md_refs=md_refs,
            )
        )
        i = k + 1
    return methods


# Сопоставление каталога выгрузки и вида объекта метаданных
_KIND_DIRS = {
    "Catalogs": "Справочник", "Documents": "Документ", "CommonModules": "ОбщийМодуль",
    "InformationRegisters": "РегистрСведений", "AccumulationRegisters": "РегистрНакопления",
    "DataProcessors": "Обработка", "Reports": "Отчет", "Enums": "Перечисление",
    "ChartsOfCharacteristicTypes": "ПланВидовХарактеристик", "CommonForms": "ОбщаяФорма",
    "ExchangePlans": "ПланОбмена", "BusinessProcesses": "БизнесПроцесс", "Tasks": "Задача",
    "DocumentJournals": "ЖурналДокументов", "Constants": "Константа",
}


def module_owner(rel_path: str) -> str:
    """Catalogs/Номенклатура/Ext/ObjectModule.bsl → Справочник.Номенклатура"""
    parts = Path(rel_path).parts
    if len(parts) >= 2 and parts[0] in _KIND_DIRS:
        return f"{_KIND_DIRS[parts[0]]}.{parts[1]}"
    if parts and parts[0] == "Ext":
        return "Конфигурация"
    return parts[0] if parts else ""


def iter_modules(dump_root: str | Path):
    root = Path(dump_root)
    for path in sorted(root.rglob("*.bsl")):
        rel = path.relative_to(root).as_posix()
        yield rel, path.read_text(encoding="utf-8-sig", errors="replace")
