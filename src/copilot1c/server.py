"""Демон ядра: HTTP API на localhost для веба, бота и MCP (copilot1c serve).

Слушает только 127.0.0.1 — наружу ядро не выставляется, клиенты ходят через веб-часть. Секреты
(.env с ключом AI Studio, DSN PostgreSQL) остаются у демона.

Методы появляются по шагам; сейчас:
  GET /health — что видит ядро: настройки AI Studio, индекс и его манифест, PostgreSQL, платформа 1С.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI

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


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="1С Project Copilot — ядро", version=__version__)
    s = settings or get_settings()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return health_report(s)

    return app
