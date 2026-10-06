"""JSON-схемы для AI Studio: строгий вид («Invalid JSON Schema: all fields must be required»)."""

import json
from types import SimpleNamespace

import pytest

from copilot1c import intent, kb
from copilot1c.index import yandex
from copilot1c.ingest import entities_llm


def _objects(schema):
    """Все объекты схемы, включая вложенные в массивы."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" or (isinstance(schema.get("type"), list) and "object" in schema["type"]):
            yield schema
        for key in ("properties",):
            for sub in (schema.get(key) or {}).values():
                yield from _objects(sub)
        if "items" in schema:
            yield from _objects(schema["items"])


@pytest.mark.parametrize("schema", [intent.SCHEMA, kb.SCHEMA, entities_llm.SCHEMA], ids=["intent", "kb", "entities"])
def test_all_fields_required_in_every_object(schema):
    strict = yandex.strict_schema(schema)
    objs = list(_objects(strict))
    assert objs
    for o in objs:
        assert set(o["required"]) == set(o["properties"]) and o["additionalProperties"] is False


def test_optional_fields_become_nullable_and_original_untouched():
    before = json.dumps(intent.SCHEMA, sort_keys=True)
    strict = yandex.strict_schema(intent.SCHEMA)
    assert strict["properties"]["reason"]["type"] == ["string", "null"]  # было необязательным
    assert strict["properties"]["intents"]["type"] == "array"            # было обязательным — без null
    k = yandex.strict_schema(kb.SCHEMA)["properties"]
    assert k["cause"]["type"] == ["string", "null"] and k["keywords"]["type"] == ["array", "null"]
    e = yandex.strict_schema(entities_llm.SCHEMA)["properties"]["entities"]["items"]["properties"]
    assert e["role"]["type"] == ["string", "null"] and e["kind"]["type"] == "string"
    assert json.dumps(intent.SCHEMA, sort_keys=True) == before


def test_chat_json_sends_strict_schema_and_drops_nulls(monkeypatch):
    seen = {}

    def create(**kw):
        seen.update(kw)
        content = json.dumps({"intents": ["issue"], "reason": None})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    monkeypatch.setattr(yandex, "client", lambda s: SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create))))
    s = SimpleNamespace(model_uri=lambda m: f"gpt://f/{m}")
    out = yandex.chat_json("текст", intent.SCHEMA, model="yandexgpt-lite/latest", settings=s)
    assert out == {"intents": ["issue"]}  # null убран — для кода это «поля нет»
    sent = seen["response_format"]["json_schema"]["schema"]
    assert sent["required"] == ["intents", "reason"] and seen["model"] == "gpt://f/yandexgpt-lite/latest"
