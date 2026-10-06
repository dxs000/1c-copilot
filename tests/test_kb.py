"""Решённое обращение → разбор для базы знаний и отправка в «Материалы»."""

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from copilot1c import kb, server
from copilot1c.config import Settings
from copilot1c.issues import IssueError

ERROR = "{Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: Поле объекта не обнаружено (КС_Гамма)"
ISSUE = {
    "id": 7, "title": "Не проводится реализация после обновления", "category": "bug",
    "description": "Дмитрий,\nМария Смирнова (m.smirnova@pierre-fabre.com, +7 495 123-45-67) пишет: реализация не "
                   "проводится у отдела продаж.",
    "error_text": ERROR, "objects": ["Документ.РеализацияТоваровУслуг", "КС_Гамма"], "config_version": "11.5.27.75",
    "root_cause": "Реквизит КС_Гамма не перенесён в расширение после обновления",
    "resolution": "Добавили реквизит КС_Гамма в расширение КС_Доработки, обновили конфигурацию базы, "
                  "перепровели документы за октябрь.",
    "test_case_ids": ["31"], "requirement_ids": ["90"], "resolved_at": "2026-10-06T10:00:00+03:00",
    "initiator": {"name": "Смирнова Мария", "email": "m.smirnova@pierre-fabre.com"},
}


def test_template_without_model_and_no_personal_data():
    d = kb.compose(ISSUE, settings=None)
    t = d["text"]
    assert d["method"] == "template" and d["title"] == ISSUE["title"]
    assert t.startswith("# Решённое обращение ОБР-0007: Не проводится реализация после обновления")
    for part in ("## Симптом", "реализация не проводится у отдела продаж", "Текст ошибки:", ERROR,
                 "## Причина", "## Решение", "перенесён в расширение", "перепровели",
                 "Тест-кейсы ПиМИ: 31", "Объекты: Документ.РеализацияТоваровУслуг, КС_Гамма", "Решено: 2026-10-06"):
        assert part in t, part
    for pii in ("Смирнова", "Мария", "m.smirnova", "123-45-67"):
        assert pii not in t, pii
    assert "Пользователь (<email@pierre-fabre.com>, <телефон>) пишет: реализация не проводится" in t


def test_resolution_required_and_missing_cause_warns():
    with pytest.raises(IssueError):
        kb.compose({**ISSUE, "resolution": " "})
    d = kb.compose({**ISSUE, "root_cause": None})
    assert "Причина не указана" in d["text"] and any("Причина" in w for w in d["warnings"])


def test_template_drops_greeting_and_analyst_address():
    d = kb.compose({**ISSUE, "error_text": None}, settings=SimpleNamespace(yc_api_key="", yc_folder_id="",
                                                                          analysts=("Дмитрий",)))
    assert "Дмитрий" not in d["text"] and "реализация не проводится" in d["text"]


def _settings():
    return SimpleNamespace(yc_api_key="k", yc_folder_id="f", model_business_text="m", analysts=())


def test_model_writeup_is_scrubbed(monkeypatch):
    from copilot1c.index import yandex

    seen = {}

    def fake(prompt, schema, **kw):
        seen["prompt"] = prompt
        return {"title": "Реализация не проводится: нет реквизита КС_Гамма",
                "symptom": "При проведении реализации — «Поле объекта не обнаружено». Сообщила Смирнова Мария.",
                "cause": "Реквизит не перенесён в расширение.", "solution": "Перенести реквизит, обновить базу.",
                "keywords": ["КС_Гамма", "проведение"]}

    monkeypatch.setattr(yandex, "chat_json", fake)
    d = kb.compose(ISSUE, settings=_settings())
    assert d["method"] == "llm" and d["title"] == "Реализация не проводится: нет реквизита КС_Гамма"
    assert "Смирнова" not in seen["prompt"] and "m.smirnova" not in seen["prompt"]  # в модель ПДн не уходят
    assert "Смирнова" not in d["text"]
    assert "Ключевые слова: Документ.РеализацияТоваровУслуг, КС_Гамма, проведение" in d["text"]


def test_model_failure_falls_back_to_template(monkeypatch):
    from copilot1c.index import yandex

    def boom(*a, **kw):
        raise TimeoutError("AI Studio")

    monkeypatch.setattr(yandex, "chat_json", boom)
    d = kb.compose(ISSUE, settings=_settings())
    assert d["method"] == "template" and any("модель недоступна" in w for w in d["warnings"])


PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from copilot1c.graph.store import GraphStore

    s = Settings(pg_dsn=PG_DSN, project="test-kb", issues_dir="issues", materials_dir="data/uploads",
                 cache_dir=str(tmp_path / ".cache"), yc_api_key="", yc_folder_id="", onec_bin="/nonexistent")
    g = GraphStore(settings=s)
    g.init_schema()
    g.conn.execute("TRUNCATE issue_events, issue_attachments, issues, contacts RESTART IDENTITY CASCADE")
    g.conn.execute("DELETE FROM materials WHERE project = 'test-kb'")
    g.conn.commit()
    g.close()
    monkeypatch.chdir(tmp_path)
    return TestClient(server.create_app(s))


@needs_pg
def test_draft_and_publish_to_materials(client, tmp_path):
    iid = client.post("/issues", json={"title": "Бланк заказа не грузится в 1С", "actor": "Иванов И."}).json()["id"]
    r = client.post(f"/issues/{iid}/kb-draft")
    assert r.status_code == 422 and "Решение" in r.json()["detail"]
    v = client.get(f"/issues/{iid}").json()["version"]
    client.patch(f"/issues/{iid}", json={"version": v, "changes": {
        "status": "resolved", "root_cause": "Бланк в старом формате xls", "resolution": "Пересохранили бланк в xlsx, "
                                                                                       "загрузка проходит"}})
    d = client.post(f"/issues/{iid}/kb-draft", json={"use_llm": False}).json()
    assert d["method"] == "template" and "Пересохранили бланк" in d["text"]

    out = client.post(f"/issues/{iid}/kb-publish", json={"title": d["title"], "text": d["text"],
                                                         "actor": "Иванов И."}).json()
    m = out["material"]
    assert m["filename"] == f"Решение ОБР-{iid:04d} — Бланк заказа не грузится в 1С.md" and m["status"] == "queued"
    assert (tmp_path / m["path"]).read_text(encoding="utf-8") == d["text"]
    issue = out["issue"]
    assert issue["kb_material_id"] == m["id"]
    assert any(e["type"] == "comment" and "базу знаний" in (e["comment"] or "") for e in issue["events"])
    # то же содержимое второй раз — тот же материал, история не дублируется
    again = client.post(f"/issues/{iid}/kb-publish", json={"title": d["title"], "text": d["text"]}).json()
    assert again["material"]["id"] == m["id"] and again["material"]["already_uploaded"] is True
    assert len(again["issue"]["events"]) == len(issue["events"])
    assert client.post("/issues/999/kb-draft").status_code == 404
