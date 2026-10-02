"""Извлечение сущностей и связей LLM со structured output (JSON-схема).

Дополняет regex-якоря из entities.py: роли участников, решения, открытые вопросы.
"""

from __future__ import annotations

from typing import Any

from copilot1c.config import Settings, get_settings
from copilot1c.index.yandex import chat_json
from copilot1c.models import Entity, EntityKind, Relation

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": [k.value for k in EntityKind]},
                    "name": {"type": "string"},
                    "role": {"type": "string", "description": "Роль участника или организации, если есть"},
                    "details": {"type": "string", "description": "Суть решения/вопроса, одна фраза"},
                },
                "required": ["kind", "name"],
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "rel": {"type": "string"},
                    "dst": {"type": "string"},
                },
                "required": ["src", "rel", "dst"],
            },
        },
    },
    "required": ["entities", "relations"],
}

SYSTEM = (
    "Ты извлекаешь сущности из проектной переписки и документов по внедрению 1С. "
    "Сущности: участник (person), организация, документ (ДС, ТЗ, ПиМИ с номером/редакцией), "
    "версия ПО (платформа 1С, конфигурация с версией), сервер, объект метаданных 1С, "
    "решение (decision), открытый вопрос (open_question), требование, тест-кейс. "
    "Связи — глаголами: decides, mentions, version_of, attached_to, asks, answers, requires, tests. "
    "src и dst в связях — имена сущностей из списка entities. Ничего не выдумывай: "
    "только то, что явно есть в тексте."
)


def extract_llm_entities(text: str, chunk_id: str | None = None,
                         settings: Settings | None = None) -> tuple[list[Entity], list[Relation]]:
    s = settings or get_settings()
    data = chat_json(text, SCHEMA, model=s.model_batch, system=SYSTEM, settings=s)
    by_name: dict[str, Entity] = {}
    for e in data.get("entities", []):
        try:
            kind = EntityKind(e["kind"])
        except (KeyError, ValueError):
            continue
        attrs = {k: v for k in ("role", "details") if (v := e.get(k))}
        by_name[e["name"]] = Entity(kind=kind, name=e["name"], attrs=attrs)
    rels = [
        Relation(src=by_name[r["src"]].key, dst=by_name[r["dst"]].key, rel=r["rel"], source_chunk=chunk_id)
        for r in data.get("relations", [])
        if r.get("src") in by_name and r.get("dst") in by_name
    ]
    return list(by_name.values()), rels
