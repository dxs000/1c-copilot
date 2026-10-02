"""Быстрое извлечение сущностей регулярными выражениями.

Работает без LLM и даёт надёжные «якоря» (версии, серверы, объекты КС_, номера документов),
которые затем дополняются LLM-извлечением со structured output (см. entities_llm.py).
"""

from __future__ import annotations

import re

from copilot1c.models import Entity, EntityKind

# Платформа 1С: 8.3.27.2342
_PLATFORM_RE = re.compile(r"\b8\.[35]\.\d{1,2}\.\d{3,5}\b")
# Версия конфигурации: УТ 11.5.27.75, 10.3.x; рядом обычно стоит имя конфигурации
_CONFIG_RE = re.compile(
    r"\b(УТ|УПП|ERP|КА|БП|ЗУП|УНФ)\s*(\d{1,2}(?:\.\d{1,3}){1,3})\b", re.IGNORECASE
)
# Нетиповые объекты интегратора: КС_Гамма, Приказы (КС)
_CUSTOM_RE = re.compile(r"\bКС_[A-Za-zА-Яа-яЁё0-9_]+|[A-Za-zА-Яа-яЁё0-9_]+\s\(КС\)")
# Документы проекта: ДС № 10, ТЗ ред. 2, ПиМИ
_DOC_RE = re.compile(
    r"\b(ДС|Доп\.?\s*соглашение)\s*№\s*(\d+)|\b(ТЗ)(?:\s*ред\.?\s*(\d+))?|\b(ПиМИ|ПМИ)\b", re.IGNORECASE
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
    for m in _CUSTOM_RE.finditer(text):
        add(Entity(kind=EntityKind.MD_OBJECT, name=m.group(0).strip(), attrs={"custom": "true"}))
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
