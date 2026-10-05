"""Демон ядра: GET /health."""

from fastapi.testclient import TestClient

from copilot1c import server
from copilot1c.config import Settings


def _client(monkeypatch, pg_ok: bool, **kw) -> TestClient:
    monkeypatch.setattr(server, "_check_postgres", lambda s: {"ok": pg_ok, "tables": 9} if pg_ok else
                        {"ok": False, "detail": "нет подключения"})
    s = Settings(yc_api_key="AQVN-test-key", yc_folder_id="b1g", vector_store_id="vs1", onec_bin="/nonexistent",
                 pg_dsn="postgresql://copilot:secret@localhost:5432/copilot", **kw)
    return TestClient(server.create_app(s))


def test_health_ok_without_platform_1c(monkeypatch, tmp_path):
    (tmp_path / "vector_store").mkdir()
    (tmp_path / "vector_store" / "vs1.json").write_text("{}")
    r = _client(monkeypatch, True, cache_dir=str(tmp_path)).get("/health")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok"  # платформа 1С необязательна
    assert body["checks"]["vector_store"]["manifest"] is True
    assert body["checks"]["platform_1c"] == {"ok": False, "optional": True}


def test_health_degraded_and_no_secrets(monkeypatch, tmp_path):
    r = _client(monkeypatch, False, cache_dir=str(tmp_path)).get("/health")
    body = r.json()
    assert body["status"] == "degraded" and body["checks"]["vector_store"]["manifest"] is False
    assert "secret" not in r.text and "AQVN-test-key" not in r.text  # ни пароля БД, ни ключа AI Studio
    assert body["checks"]["postgres"]["host"] == "localhost:5432/copilot"
