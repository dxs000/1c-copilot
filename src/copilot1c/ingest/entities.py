"""Быстрое извлечение сущностей регулярными выражениями.

Работает без LLM и даёт надёжные «якоря» (версии, серверы, объекты КС_, номера документов),
которые затем дополняются LLM-извлечением со structured output (см. entities_llm.py).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from copilot1c.models import Entity, EntityKind

# Платформа 1С: 8.3.27.2342
_PLATFORM_RE = re.compile(r"\b8\.[35]\.\d{1,2}\.\d{3,5}\b")
# Версия конфигурации: УТ 11.5.27.75, 10.3.x; рядом обычно стоит имя конфигурации
_CONFIG_RE = re.compile(
    r"\b(УТ|УПП|ERP|КА|БП|ЗУП|УНФ)\s*(\d{1,2}(?:\.\d{1,3}){1,3})\b", re.IGNORECASE
)
# Нетиповые объекты интегратора: КС_Гамма, Приказы (КС)
_CUSTOM_RE = re.compile(r"\bКС_[A-Za-zА-Яа-яЁё0-9_]+|[«\"]([^«»\"]{2,80}\(КС\))[»\"]|\b[А-ЯЁ][\wЁё]+\s\(КС\)")
# Версия без имени конфигурации: «до версии 11.5.27.75» (продукт берётся из ближайшего упоминания слева)
_BARE_VERSION_RE = re.compile(r"верси[июяей]\s+(\d{1,2}\.\d{1,2}\.\d{1,3}\.\d{1,4})", re.IGNORECASE)
_PRODUCT_RE = re.compile(r"\b(УТ|УПП|ERP|КА|БП|ЗУП|УНФ)(?=\s*\d|\b)|(Управление\s+торговлей|Бухгалтерия\s+предприятия|"
                         r"Управление\s+производственным\s+предприятием|Комплексная\s+автоматизация|"
                         r"Зарплата\s+и\s+управление\s+персоналом|Управление\s+нашей\s+фирмой)", re.IGNORECASE)
_PRODUCT_NAMES = {"управление торговлей": "УТ", "бухгалтерия предприятия": "БП",
                  "управление производственным предприятием": "УПП", "комплексная автоматизация": "КА",
                  "зарплата и управление персоналом": "ЗУП", "управление нашей фирмой": "УНФ"}


def _product(m: re.Match[str]) -> str:
    if m.group(1):
        return m.group(1).upper()
    return _PRODUCT_NAMES[re.sub(r"\s+", " ", m.group(2)).casefold()]
# Документы проекта: ДС № 10, Дополнительного соглашения №10, ТЗ ред. 2, ПиМИ
_DOC_RE = re.compile(
    r"\b(ДС|доп\w*\.?\s*соглашени\w*)\s*№\s*(\d+)|\b(ТЗ)(?:\s*ред\.?\s*(\d+))?|\b(ПиМИ|ПМИ)\b",
    re.IGNORECASE,
)
# Имена серверов: буквенный префикс + роль + номер (pfmosvt1ceapp01, srv-1c-sql02)
_SERVER_RE = re.compile(r"\b[a-z][a-z0-9-]{2,}(?:app|srv|sql|db|web|ras|apl)[0-9]{1,3}\b", re.IGNORECASE)


def extract_regex_entities(text: str) -> list[Entity]:
    found: dict[str, Entity] = {}

    def add(e: Entity) -> None:
        found.setdefault(e.key, e)

    for m in _PLATFORM_RE.finditer(text):
        add(Entity(kind=EntityKind.SOFTWARE_VERSION, name=f"Платформа {m.group(0)}",
                   attrs={"product": "Платформа 1С", "version": m.group(0)}))
    for m in _CONFIG_RE.finditer(text):
        prod, ver = m.group(1).upper(), m.group(2)
        if prod in {"УТ"} and ver.count(".") == 0:
            continue
        add(Entity(kind=EntityKind.SOFTWARE_VERSION, name=f"{prod} {ver}",
                   attrs={"product": prod, "version": ver}))
    for m in _BARE_VERSION_RE.finditer(text):
        ver = m.group(1)
        if ver.startswith(("8.3.", "8.5.")):
            continue
        products = [_product(p) for p in _PRODUCT_RE.finditer(text[: m.start()])]
        prod = products[-1] if products else "Конфигурация"
        add(Entity(kind=EntityKind.SOFTWARE_VERSION, name=f"{prod} {ver}", attrs={"product": prod, "version": ver}))
    for m in _CUSTOM_RE.finditer(text):
        name = (m.group(1) or m.group(0)).strip()
        add(Entity(kind=EntityKind.MD_OBJECT, name=re.sub(r"\s+", " ", name), attrs={"custom": "true"}))
    for m in _DOC_RE.finditer(text):
        if m.group(2):
            add(Entity(kind=EntityKind.DOCUMENT, name=f"ДС № {m.group(2)}", attrs={"doc_type": "ds"}))
        elif m.group(3):
            name = f"ТЗ ред. {m.group(4)}" if m.group(4) else "ТЗ"
            add(Entity(kind=EntityKind.DOCUMENT, name=name, attrs={"doc_type": "tz"}))
        elif m.group(5):
            add(Entity(kind=EntityKind.DOCUMENT, name="ПиМИ", attrs={"doc_type": "pimi"}))
    for m in _SERVER_RE.finditer(text):
        add(Entity(kind=EntityKind.SERVER, name=m.group(0).lower()))
    return list(found.values())


def is_custom_object(name: str, prefixes: tuple[str, ...] = ("КС_", "(КС)")) -> bool:
    """Объект доработан интегратором (не типовой) — рискует при обновлении."""
    return any(name.startswith(p) or name.endswith(p) for p in prefixes)


# Объекты метаданных в тексте документов
_MD_IDENT_RE = re.compile(r"\bКС_[\wЁё]+|\b[А-ЯЁ][а-яё]{2,}(?:[А-ЯЁ][а-яё]{2,}|[А-ЯЁ]{2,}\b)+")
_KIND_STEMS = [
    (r"справочник\w*", "Справочник"),
    (r"документ(?:а|ы|ов|е|ом)?", "Документ"),
    (r"регистр\w*\s+сведений", "РегистрСведений"),
    (r"регистр\w*\s+накоплени\w*", "РегистрНакопления"),
    (r"обработк\w*", "Обработка"),
    (r"отч[её]т\w*", "Отчет"),
    (r"перечислени\w*", "Перечисление"),
]
_KIND_QUOTED_RE = re.compile(
    r"\b(" + "|".join(f"(?:{p})" for p, _ in _KIND_STEMS) + r")\s+[«\"„]([^»\"“]{2,60})[»\"“]", re.IGNORECASE
)


def _kind_of(word: str) -> str:
    for pattern, kind in _KIND_STEMS:
        if re.fullmatch(pattern, word, re.IGNORECASE):
            return kind
    return ""


def find_md_objects(text: str, known: Iterable[str] | None = None) -> list[str]:
    """Упоминания объектов 1С: «справочник "Номенклатура"» → «Справочник «Номенклатура»»,
    идентификаторы вида ШтрихкодыНоменклатуры и КС_Гамма, а также имена из реестра known."""
    found: set[str] = set()
    for m in _KIND_QUOTED_RE.finditer(text):
        kind = _kind_of(re.sub(r"\s+", " ", m.group(1)))
        name = m.group(2).strip()
        if kind and name:
            found.add(f"{kind} «{name[0].upper() + name[1:]}»")
    found.update(m.group(0) for m in _MD_IDENT_RE.finditer(text))
    for name in known or ():
        if re.search(rf"(?<![\wЁё]){re.escape(name)}(?![\wЁё])", text):
            found.add(name)
    return sorted(found)
