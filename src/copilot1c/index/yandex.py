"""Клиент Yandex AI Studio через OpenAI-совместимый API.

Чат, эмбеддинги, Files и Vector Stores идут через один клиент openai с base_url AI Studio
и project = идентификатор каталога. Имена моделей задаются в config.Settings.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Any

from openai import OpenAI

from copilot1c.config import Settings, get_settings
from copilot1c.models import Chunk


@lru_cache
def _client(api_key: str, base_url: str, folder_id: str) -> OpenAI:
    return OpenAI(api_key=api_key, base_url=base_url, project=folder_id)


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
        out.append(c.embeddings.create(model=model, input=text, encoding_format="float").data[0].embedding)
    return out


def chat_json(prompt: str, schema: dict[str, Any], *, model: str, system: str = "",
              settings: Settings | None = None) -> dict[str, Any]:
    """Вызов модели со structured output по JSON-схеме."""
    s = settings or get_settings()
    messages = [{"role": "system", "content": system}] if system else []
    messages.append({"role": "user", "content": prompt})
    resp = client(s).chat.completions.create(
        model=s.model_uri(model),
        messages=messages,
        temperature=0,
        response_format={"type": "json_schema", "json_schema": {"name": "result", "schema": schema}},
    )
    return json.loads(resp.choices[0].message.content or "{}")


class VectorIndex:
    """Индекс в AI Studio Vector Store: загрузка чанков как файлов с атрибутами для фильтров."""

    def __init__(self, vector_store_id: str, settings: Settings | None = None):
        self.id = vector_store_id
        self.s = settings or get_settings()

    @classmethod
    def create(cls, name: str, settings: Settings | None = None) -> VectorIndex:
        s = settings or get_settings()
        vs = client(s).vector_stores.create(name=name)
        return cls(vs.id, s)

    def add(self, chunks: Iterable[Chunk]) -> list[str]:
        c = client(self.s)
        ids: list[str] = []
        for ch in chunks:
            f = c.files.create(file=(f"{ch.chunk_id}.md", ch.text.encode("utf-8")), purpose="assistants")
            c.vector_stores.files.create(vector_store_id=self.id, file_id=f.id, attributes=ch.attributes())
            ids.append(f.id)
        return ids

    def search(self, query: str, *, filters: dict[str, str] | None = None, k: int = 10) -> list[dict[str, Any]]:
        flt = None
        if filters:
            parts = [{"type": "eq", "key": key, "value": val} for key, val in filters.items()]
            flt = parts[0] if len(parts) == 1 else {"type": "and", "filters": parts}
        kwargs: dict[str, Any] = {"query": query, "max_num_results": k}
        if flt:
            kwargs["filters"] = flt
        res = client(self.s).vector_stores.search(self.id, **kwargs)
        return [
            {
                "score": r.score,
                "file_id": r.file_id,
                "attributes": r.attributes,
                "text": "\n".join(part.text for part in r.content),
            }
            for r in res.data
        ]
