"""Инструменты агента-оркестратора и цикл вызова инструментов.

Описания инструментов в формате function calling (OpenAI-совместимый Chat Completions AI Studio).
Эти же функции можно выставить наружу MCP-сервером для IDE.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from copilot1c.code1c.platform import Designer
from copilot1c.config import Settings, get_settings
from copilot1c.graph.store import GraphStore
from copilot1c.index.yandex import VectorIndex, client

SYSTEM_PROMPT = """Ты — 1С Project Copilot, аналитик проекта внедрения 1С.
Отвечай по-русски. Каждое утверждение подкрепляй источником: письмо (дата, автор), пункт ТЗ,
№ тест-кейса ПиМИ, путь к модулю и строки. Утверждение без источника помечай как предположение.
Аналитические вопросы («что не покрыто», «что затронет обновление») решай в несколько шагов:
поиск → граф → SQL. Объекты с префиксом КС_ или суффиксом (КС) — доработки интегратора.
Сгенерированный код перед выдачей проверяй инструментом build_and_check."""


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


_STR = {"type": "string"}
TOOLS = [
    _fn("search_docs", "Гибридный поиск по переписке, ТЗ, ДС, ПиМИ с фильтрами метаданных.",
        {"query": _STR, "doc_type": {"type": "string", "enum": ["email", "tz", "ds", "pimi", "doc"]},
         "k": {"type": "integer"}}, ["query"]),
    _fn("search_code", "Поиск методов BSL и карточек объектов по описанию или имени.",
        {"query": _STR, "config": {"type": "string", "description": "Метка выгрузки, напр. «УТ 11.5.27.75»"},
         "custom_only": {"type": "boolean"}, "k": {"type": "integer"}}, ["query"]),
    _fn("graph_query", "Связи сущности в графе: кто упоминает, какие тест-кейсы проверяют, кто вызывает.",
        {"entity": {"type": "string", "description": "Ключ вида kind:name или имя объекта/метода"},
         "rel": _STR}, ["entity"]),
    _fn("get_module", "Полный текст модуля BSL из выгрузки.",
        {"config": _STR, "module": {"type": "string", "description": "Путь вида Catalogs/X/Ext/ObjectModule.bsl"}},
        ["config", "module"]),
    _fn("diff_versions", "Дифф двух выгрузок конфигурации в Git (теги/коммиты) по пути.",
        {"base": _STR, "target": _STR, "path": _STR}, ["base", "target"]),
    _fn("sql", "SELECT-запрос только на чтение к реестрам: test_cases, requirements, md_objects, "
        "bsl_methods, bsl_calls, uncovered_requirements, custom_objects.", {"query": _STR}, ["query"]),
    _fn("build_and_check", "Собрать расширение из XML/BSL в песочнице 1С и проверить /CheckModules.",
        {"src_dir": _STR, "extension": _STR}, ["src_dir", "extension"]),
]


@dataclass
class ToolContext:
    settings: Settings
    vector_store_id: str
    dumps_root: Path  # Git-репозиторий выгрузок: <dumps_root>/<config>/...
    store: GraphStore | None = None


def _search(ctx: ToolContext, query: str, filters: dict[str, str], k: int) -> list[dict]:
    return VectorIndex(ctx.vector_store_id, ctx.settings).search(query, filters=filters, k=k)


def make_handlers(ctx: ToolContext) -> dict[str, Callable[..., Any]]:
    project = ctx.settings.project

    def search_docs(query: str, doc_type: str | None = None, k: int = 8):
        f = {"project": project}
        if doc_type:
            f["doc_type"] = doc_type
        return _search(ctx, query, f, k)

    def search_code(query: str, config: str | None = None, custom_only: bool = False, k: int = 8):
        f = {"project": project, "doc_type": "bsl_method"}
        if config:
            f["doc_version"] = config
        if custom_only:
            f["custom"] = "true"
        return _search(ctx, query, f, k)

    def graph_query(entity: str, rel: str | None = None):
        assert ctx.store, "graph store not configured"
        key = entity if ":" in entity else None
        sql = """SELECT r.src, r.rel, r.dst, c.source, c.title FROM relations r
                 LEFT JOIN chunks c ON c.chunk_id = r.source_chunk
                 WHERE (r.src = %(k)s OR r.dst = %(k)s OR r.src ILIKE %(n)s OR r.dst ILIKE %(n)s)
                   AND (%(rel)s::text IS NULL OR r.rel = %(rel)s) LIMIT 100"""
        return ctx.store.query(sql, {"k": key, "n": f"%:{entity.casefold()}", "rel": rel})

    def get_module(config: str, module: str):
        path = (ctx.dumps_root / config / module).resolve()
        if not path.is_relative_to(ctx.dumps_root.resolve()):
            return {"error": "путь вне каталога выгрузок"}
        return path.read_text(encoding="utf-8-sig", errors="replace")

    def diff_versions(base: str, target: str, path: str = "."):
        out = subprocess.run(["git", "-C", str(ctx.dumps_root), "diff", "--stat", base, target, "--", path],
                             capture_output=True, text=True, timeout=120)
        return out.stdout[-20000:] or out.stderr

    def sql(query: str):
        assert ctx.store, "graph store not configured"
        if not query.lstrip().lower().startswith(("select", "with")):
            return {"error": "разрешены только SELECT-запросы"}
        return ctx.store.query(query)

    def build_and_check(src_dir: str, extension: str):
        out = Path(src_dir).with_suffix(".cfe")
        results = Designer(ctx.settings).build_extension(src_dir, out, extension)
        return [{"step": r.argv[r.argv.index("/Out") + 2], "ok": r.ok, "log": r.log[-4000:]} for r in results]

    return {f.__name__: f for f in (search_docs, search_code, graph_query, get_module, diff_versions, sql,
                                     build_and_check)}


def ask(question: str, ctx: ToolContext, max_steps: int = 8) -> str:
    """Цикл агента: модель вызывает инструменты, пока не даст финальный ответ."""
    s = ctx.settings or get_settings()
    handlers = make_handlers(ctx)
    # Без PostgreSQL инструменты графа и реестров не предлагаются модели вовсе
    unavailable = set() if ctx.store is not None else {"graph_query", "sql"}
    if not ctx.dumps_root.exists():
        unavailable |= {"get_module", "diff_versions"}
    tools = [t for t in TOOLS if t["function"]["name"] not in unavailable]
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT},
                                      {"role": "user", "content": question}]
    for _ in range(max_steps):
        resp = client(s).chat.completions.create(model=s.model_uri(s.model_orchestrator), messages=messages,
                                                 tools=tools, temperature=0.1)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""
        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            try:
                args = json.loads(call.function.arguments or "{}")
                result = handlers[call.function.name](**args)
            except Exception as exc:  # ошибка инструмента возвращается модели, а не роняет цикл
                result = {"error": f"{type(exc).__name__}: {exc}"}
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(result, ensure_ascii=False, default=str)[:30000]})
    return "Не удалось получить ответ за отведённое число шагов."
