"""Демон ядра: HTTP API на localhost для веба, бота и MCP (copilot1c serve).

Слушает только 127.0.0.1 — наружу ядро не выставляется, клиенты ходят через веб-часть. Секреты
(.env с ключом AI Studio, DSN PostgreSQL) остаются у демона.

Методы появляются по шагам; сейчас:
  GET /health — что видит ядро: настройки AI Studio, индекс и его манифест, PostgreSQL, платформа 1С.
  POST /ask  — вопрос агенту: ответ, найденные фрагменты-источники, шаги агента. Формат ответа совпадает
               с /api/ask веб-части, чтобы веб проксировал запрос без изменений интерфейса.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from copilot1c import __version__
from copilot1c.config import Settings, get_settings


def _check_postgres(s: Settings) -> dict[str, Any]:
    from copilot1c.graph.store import try_connect

    g = try_connect(s)
    if g is None:
        return {"ok": False, "detail": "нет подключения"}
    try:
        tables = g.query("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public'")
        return {"ok": True, "tables": tables[0]["n"] if tables else 0}
    except Exception as exc:  # noqa: BLE001 — health не должен падать из-за схемы
        return {"ok": False, "detail": f"{type(exc).__name__}"}
    finally:
        g.close()


def health_report(s: Settings) -> dict[str, Any]:
    manifest = Path(s.cache_dir or ".cache") / "vector_store" / f"{s.vector_store_id}.json"
    checks = {
        "ai_studio": {"ok": bool(s.yc_api_key and s.yc_folder_id), "folder": s.yc_folder_id or None},
        "vector_store": {"ok": bool(s.vector_store_id), "id": s.vector_store_id or None,
                         "manifest": manifest.exists() if s.vector_store_id else False},
        "postgres": {**_check_postgres(s), "host": s.pg_dsn.rsplit("@", 1)[-1]},  # без логина и пароля
        "platform_1c": {"ok": Path(s.onec_bin).exists(), "optional": True},
    }
    required_ok = all(checks[k]["ok"] for k in ("ai_studio", "vector_store", "postgres"))
    return {"status": "ok" if required_ok else "degraded", "version": __version__, "project": s.project,
            "checks": checks}


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


def search_sources(s: Settings, question: str, k: int = 8) -> list[dict[str, Any]]:
    """Те же фрагменты, что агент получает перед ответом (тот же запрос и тот же поиск ядра)."""
    from copilot1c.index.yandex import VectorIndex
    from copilot1c.retrieval import smart_search, source_label

    index = VectorIndex(s.vector_store_id, s)
    hits = smart_search(lambda q, f, kk: index.search(q, filters=f, k=kk), question, {"project": s.project}, k)
    out = []
    for i, h in enumerate(hits, 1):
        attrs = h.get("attributes") or {}
        out.append({"n": i, "label": source_label(attrs, h.get("text", "")), "doc_type": attrs.get("doc_type", ""),
                    "date": attrs.get("date", ""), "text": h.get("text", "")})
    return out


def run_question(s: Settings, question: str):
    from copilot1c.agent.tools import ToolContext, run_agent
    from copilot1c.graph.store import try_connect

    store = try_connect(s)
    try:
        return run_agent(question, ToolContext(s, s.vector_store_id, Path("data/dumps"), store))
    finally:
        if store is not None:
            store.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="1С Project Copilot — ядро", version=__version__)
    s = settings or get_settings()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return health_report(s)

    @app.post("/ask")
    def ask(req: AskRequest) -> dict[str, Any]:
        # Синхронный обработчик: FastAPI выполняет его в пуле потоков, долгий ответ агента не блокирует /health
        if not (s.yc_api_key and s.yc_folder_id and s.vector_store_id):
            raise HTTPException(503, "Не настроены ключи Yandex или COPILOT_VECTOR_STORE_ID в .env ядра")
        question = req.question.strip()
        t0 = time.monotonic()
        try:
            sources = search_sources(s, question)
            result = run_question(s, question)
        except Exception as exc:  # noqa: BLE001 — клиенту причина текстом, а не 500 без объяснения
            raise HTTPException(502, f"Ошибка обращения к Yandex AI Studio: {type(exc).__name__}: {str(exc)[:300]}") \
                from exc
        return {"answer": result.answer, "sources": sources, "seconds": round(time.monotonic() - t0, 1),
                "steps": result.steps, "tools": [t.get("tool", "") for t in result.trace]}

    return app
