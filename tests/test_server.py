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
    (tmp_path / "vector_store" / "vs1.json").write_text('{"a": "f1", "b": "f2"}')
    r = _client(monkeypatch, True, cache_dir=str(tmp_path), ocr_backend="none").get("/health")
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok"  # платформа 1С необязательна
    assert body["checks"]["vector_store"]["manifest"] is True and body["checks"]["vector_store"]["chunks"] == 2
    assert body["checks"]["ai_studio"]["model"] and body["checks"]["ocr"]["backend"] == "none"
    assert body["checks"]["platform_1c"] == {"ok": False, "optional": True}


def test_health_degraded_and_no_secrets(monkeypatch, tmp_path):
    r = _client(monkeypatch, False, cache_dir=str(tmp_path)).get("/health")
    body = r.json()
    assert body["status"] == "degraded" and body["checks"]["vector_store"]["manifest"] is False
    assert "secret" not in r.text and "AQVN-test-key" not in r.text  # ни пароля БД, ни ключа AI Studio
    assert body["checks"]["postgres"]["host"] == "localhost:5432/copilot"


def _ask_client(monkeypatch, **kw):
    s = Settings(yc_api_key="AQVN-test-key", yc_folder_id="b1g", vector_store_id="vs1", onec_bin="/nonexistent",
                 intent_llm=False, **kw)  # без вызова модели для типа сообщения
    return TestClient(server.create_app(s))


def test_ask_returns_web_compatible_payload(monkeypatch):
    from types import SimpleNamespace

    asked = []
    monkeypatch.setattr(server, "search_sources", lambda s, q, k=8: [
        {"n": 1, "label": "письмо «Re: ТЗ», 2026-09-15", "doc_type": "email", "date": "2026-09-15", "text": "LTS 11.5.27"}])
    monkeypatch.setattr(server, "run_question", lambda s, q: asked.append(q) or SimpleNamespace(
        answer="Нужна LTS 11.5.27", steps=2, trace=[{"tool": "search_docs", "args": "{}"}]))
    r = _ask_client(monkeypatch).post("/ask", json={"question": "  Почему не 11.6?  "})
    body = r.json()
    assert r.status_code == 200 and asked == ["Почему не 11.6?"]
    # формат /api/ask веба + тип сообщения и черновик обращения (для вопроса — пусто)
    assert set(body) == {"answer", "sources", "seconds", "steps", "tools", "intent", "issue_draft", "web_sources",
                         "escalation"}
    assert body["intent"]["primary"] == "question" and body["issue_draft"] is None
    assert body["sources"][0]["label"].startswith("письмо") and body["tools"] == ["search_docs"] and body["steps"] == 2


def test_ask_errors(monkeypatch):
    c = TestClient(server.create_app(Settings(yc_api_key="", yc_folder_id="", vector_store_id="")))
    assert c.post("/ask", json={"question": "Почему не 11.6?"}).status_code == 503
    assert c.post("/ask", json={"question": "?"}).status_code == 422  # слишком короткий вопрос

    def boom(s, q, k=8):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(server, "search_sources", boom)
    r = _ask_client(monkeypatch).post("/ask", json={"question": "Почему не 11.6?"})
    assert r.status_code == 502 and "429" in r.json()["detail"]


def test_check_postgres_counts_and_missing_schema(monkeypatch):
    from copilot1c.graph import store

    class FakeStore:
        def __init__(self, schema: bool):
            self.schema = schema

        def query(self, sql, params=()):
            if "information_schema" in sql:
                return [{"n": 12 if self.schema else 0}]
            if not self.schema:
                raise RuntimeError("relation chunks does not exist")
            return [{"chunks": 1100, "test_cases": 68, "requirements": 341}]

        def close(self):
            pass

    monkeypatch.setattr(store, "try_connect", lambda s=None: FakeStore(True))
    assert server._check_postgres(Settings()) == {"ok": True, "tables": 12, "chunks": 1100, "test_cases": 68,
                                                  "requirements": 341}
    monkeypatch.setattr(store, "try_connect", lambda s=None: FakeStore(False))
    r = server._check_postgres(Settings())
    assert r["ok"] is True and r["tables"] == 0 and "init-db" in r["detail"]
