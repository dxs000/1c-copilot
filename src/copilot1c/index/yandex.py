"""Клиент Yandex AI Studio через OpenAI-совместимый API.

Чат, эмбеддинги, Files и Vector Stores идут через один клиент openai с base_url AI Studio
и project = идентификатор каталога. Имена моделей задаются в config.Settings.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from openai import OpenAI

from copilot1c.config import Settings, get_settings
from copilot1c.models import Chunk


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
        out.append(c.embeddings.create(model=model, input=text, encoding_format="float").data[0].embedding)
    return out


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

    # --- загрузка с манифестом: повторный запуск не загружает те же чанки второй раз ---

    @property
    def manifest_path(self) -> Path:
        return Path(self.s.cache_dir or ".cache") / "vector_store" / f"{self.id}.json"

    def load_manifest(self) -> dict[str, str]:
        """chunk_id → file_id уже загруженных чанков."""
        if self.manifest_path.exists():
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        return {}

    def _save_manifest(self, manifest: dict[str, str]) -> None:
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(self.manifest_path)

    def rebuild_manifest(self, progress: Callable[[str], None] = print) -> dict[str, str]:
        """Восстанавливает манифест по файлам, уже лежащим в индексе (имя файла = chunk_id.md).

        Нужен, если чанки загружались без манифеста (например, запуск упал после загрузки)."""
        c = client(self.s)
        manifest: dict[str, str] = {}
        for vf in c.vector_stores.files.list(vector_store_id=self.id, limit=100):
            attrs = getattr(vf, "attributes", None) or {}
            chunk_id = attrs.get("chunk_id")
            if not chunk_id:
                name = c.files.retrieve(vf.id).filename or ""
                chunk_id = name.removesuffix(".md")
            if chunk_id:
                manifest[chunk_id] = vf.id
        self._save_manifest(manifest)
        progress(f"В индексе уже есть чанков: {len(manifest)}")
        return manifest

    def add(self, chunks: Iterable[Chunk], progress: Callable[[str], None] | None = None,
            every: int = 25) -> dict[str, str]:
        """Загружает чанки, которых ещё нет в индексе. Возвращает chunk_id → file_id для всех чанков."""
        c = client(self.s)
        manifest = self.load_manifest()
        chunks = list(chunks)
        todo = [ch for ch in chunks if ch.chunk_id not in manifest]
        if progress and len(todo) < len(chunks):
            progress(f"Уже в индексе: {len(chunks) - len(todo)}, загружаю новых: {len(todo)}")
        for i, ch in enumerate(todo, 1):
            f = c.files.create(file=(f"{ch.chunk_id}.md", ch.text.encode("utf-8")), purpose="assistants")
            c.vector_stores.files.create(vector_store_id=self.id, file_id=f.id,
                                         attributes={**ch.attributes(), "chunk_id": ch.chunk_id})
            manifest[ch.chunk_id] = f.id
            if i % every == 0 or i == len(todo):
                self._save_manifest(manifest)  # после сбоя загрузка продолжится с этого места
                if progress:
                    progress(f"  загружено {i}/{len(todo)}")
        return {ch.chunk_id: manifest[ch.chunk_id] for ch in chunks}

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
