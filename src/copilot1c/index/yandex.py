"""Клиент Yandex AI Studio через OpenAI-совместимый API.

Чат и эмбеддинги идут через один клиент openai с base_url AI Studio и project = идентификатор каталога.
Имена моделей задаются в config.Settings. Поиск по базе проекта — в PostgreSQL (search.py), Vector Store
AI Studio больше не используется: там один фрагмент — один файл при лимите 10 000 файлов на индекс.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from openai import OpenAI

from copilot1c.config import Settings, get_settings


@lru_cache
def _client(api_key: str, base_url: str, folder_id: str) -> OpenAI:
    # SDK сам повторяет 429/5xx с экспоненциальной паузой; квоты AI Studio невысокие — повторов больше умолчания
    return OpenAI(api_key=api_key, base_url=base_url, project=folder_id, max_retries=6, timeout=120)


def client(settings: Settings | None = None) -> OpenAI:
    s = settings or get_settings()
    if not s.yc_api_key or not s.yc_folder_id:
        raise RuntimeError("Задайте COPILOT_YC_API_KEY и COPILOT_YC_FOLDER_ID (см. .env.example)")
    return _client(s.yc_api_key, s.ai_base_url, s.yc_folder_id)


def embed(texts: Sequence[str], *, query: bool = False, settings: Settings | None = None) -> list[list[float]]:
    """Эмбеддинги: раздельные модели для документов и запросов."""
    s = settings or get_settings()
    model = s.embedding_uri(s.embedding_query if query else s.embedding_doc)
    c = client(s)
    out: list[list[float]] = []
    for text in texts:  # модели AI Studio принимают по одному тексту за вызов
        out.append(_embed_one(c, model, text))
    return out


EMBED_MAX_CHARS = 6000  # модель эмбеддингов принимает не больше 2048 токенов; длинное письмо целиком не влезет


def _embed_one(c, model: str, text: str) -> list[float]:
    """Эмбеддинг по началу текста: если модель отвечает «слишком много токенов», текст укорачивается и запрос
    повторяется. Вектор описывает начало фрагмента; полнотекстовый поиск всё равно идёт по всему тексту."""
    from openai import BadRequestError

    text = text[:EMBED_MAX_CHARS]
    for _ in range(6):
        try:
            return c.embeddings.create(model=model, input=text, encoding_format="float").data[0].embedding
        except BadRequestError as exc:
            if "token" not in str(exc).lower() or len(text) < 200:
                raise
            text = text[: int(len(text) * 0.6)]
    return c.embeddings.create(model=model, input=text, encoding_format="float").data[0].embedding


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Схема в строгом виде, который требует AI Studio («Invalid JSON Schema: all fields must be required»):
    в каждом объекте все поля перечислены в required и лишние запрещены; поле, которое было необязательным,
    допускает null — модель оставляет его пустым, а не выдумывает. Исходная схема не меняется."""
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k not in ("properties", "items")}
    if "items" in schema:
        out["items"] = strict_schema(schema["items"])
    if schema.get("type") == "object" and "properties" in schema:
        required = set(schema.get("required") or [])
        props = {}
        for name, sub in schema["properties"].items():
            sub = strict_schema(sub)
            if name not in required:
                sub = _nullable(sub)
            props[name] = sub
        out["properties"] = props
        out["required"] = list(props)
        out["additionalProperties"] = False
    return out


def _nullable(sub: dict[str, Any]) -> dict[str, Any]:
    t = sub.get("type")
    if isinstance(t, str) and t != "null":
        sub = {**sub, "type": [t, "null"]}
        if "enum" in sub:
            sub["enum"] = [*sub["enum"], None]
    elif isinstance(t, list) and "null" not in t:
        sub = {**sub, "type": [*t, "null"]}
    return sub


def chat_json(prompt: str, schema: dict[str, Any], *, model: str, system: str = "",
              settings: Settings | None = None) -> dict[str, Any]:
    """Вызов модели со structured output по JSON-схеме (схема приводится к строгому виду, см. strict_schema).
    Пустые (null) необязательные поля из ответа убираются — вызывающий код видит их как отсутствующие."""
    s = settings or get_settings()
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})
    resp = client(s).chat.completions.create(
        model=s.model_uri(model),
        messages=messages,
        temperature=0,
        response_format={"type": "json_schema",
                         "json_schema": {"name": "result", "schema": strict_schema(schema)}},
    )
    return _drop_nulls(json.loads(resp.choices[0].message.content or "{}"))


def _drop_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _drop_nulls(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_nulls(v) for v in value]
    return value
