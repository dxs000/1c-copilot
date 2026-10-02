"""Карточки объектов метаданных из XML-выгрузки конфигурации/расширения."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from copilot1c.code1c.bsl import _KIND_DIRS
from copilot1c.ingest.entities import is_custom_object

_XML_KIND = {"Catalog": "Справочник", "Document": "Документ", "CommonModule": "ОбщийМодуль",
             "InformationRegister": "РегистрСведений", "AccumulationRegister": "РегистрНакопления",
             "DataProcessor": "Обработка", "Report": "Отчет", "Enum": "Перечисление",
             "ChartOfCharacteristicTypes": "ПланВидовХарактеристик", "CommonForm": "ОбщаяФорма",
             "ExchangePlan": "ПланОбмена", "Constant": "Константа", "BusinessProcess": "БизнесПроцесс",
             "Task": "Задача", "DocumentJournal": "ЖурналДокументов"}


def _local(el) -> str:
    return etree.QName(el).localname


def _first(el, *path: str):
    cur = el
    for name in path:
        if cur is None:
            return None
        cur = next((c for c in cur if isinstance(c.tag, str) and _local(c) == name), None)
    return cur


def _text(el) -> str:
    return (el.text or "").strip() if el is not None else ""


def _synonym(props) -> str:
    syn = _first(props, "Synonym")
    if syn is None:
        return ""
    for item in syn:
        if _local(item) == "item" and _text(_first(item, "lang")) in ("ru", ""):
            return _text(_first(item, "content"))
    return ""


@dataclass
class Attribute:
    name: str
    synonym: str
    types: list[str]


@dataclass
class MetadataObject:
    kind: str
    name: str
    synonym: str = ""
    adopted: bool = False  # заимствован в расширение
    attributes: list[Attribute] = field(default_factory=list)
    tabular_sections: dict[str, list[Attribute]] = field(default_factory=dict)
    source: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.kind}.{self.name}"

    @property
    def custom(self) -> bool:
        return is_custom_object(self.name)

    def card(self) -> str:
        lines = [f"{self.full_name}" + (f" «{self.synonym}»" if self.synonym else "")]
        if self.adopted:
            lines.append("Заимствован в расширение")
        if self.custom:
            lines.append("Доработка интегратора (КС)")
        for a in self.attributes:
            lines.append(f"Реквизит {a.name}" + (f" «{a.synonym}»" if a.synonym else "")
                         + (f": {', '.join(a.types)}" if a.types else ""))
        for ts, attrs in self.tabular_sections.items():
            lines.append(f"Табличная часть {ts}: " + ", ".join(a.name for a in attrs))
        return "\n".join(lines)


def _attributes(container) -> list[Attribute]:
    out = []
    for el in container if container is not None else []:
        if not isinstance(el.tag, str) or _local(el) != "Attribute":
            continue
        props = _first(el, "Properties")
        type_el = _first(props, "Type")
        types = [_text(t) for t in (type_el if type_el is not None else [])
                 if isinstance(t.tag, str) and _local(t) == "Type"]
        out.append(Attribute(name=_text(_first(props, "Name")), synonym=_synonym(props), types=types))
    return out


def parse_object_xml(path: str | Path) -> MetadataObject | None:
    root = etree.parse(str(path)).getroot()
    obj = next((c for c in root if isinstance(c.tag, str)), None)
    if obj is None:
        return None
    kind = _XML_KIND.get(_local(obj), _local(obj))
    props = _first(obj, "Properties")
    md = MetadataObject(
        kind=kind,
        name=_text(_first(props, "Name")),
        synonym=_synonym(props),
        adopted=_text(_first(props, "ObjectBelonging")) == "Adopted",
        source=str(path),
    )
    children = _first(obj, "ChildObjects")
    md.attributes = _attributes(children)
    for el in children if children is not None else []:
        if isinstance(el.tag, str) and _local(el) == "TabularSection":
            name = _text(_first(el, "Properties", "Name"))
            md.tabular_sections[name] = _attributes(_first(el, "ChildObjects"))
    return md


def iter_objects(dump_root: str | Path):
    """Файлы объектов лежат как <КаталогВида>/<Имя>.xml рядом с одноимённой папкой."""
    root = Path(dump_root)
    for kind_dir in _KIND_DIRS:
        d = root / kind_dir
        if not d.is_dir():
            continue
        for xml in sorted(d.glob("*.xml")):
            md = parse_object_xml(xml)
            if md and md.name:
                yield md
