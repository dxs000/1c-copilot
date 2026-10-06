"""Обращения: реестр и API демона.

Тесты с базой идут на живом PostgreSQL из COPILOT_TEST_PG_DSN (таблицы обращений и контактов очищаются);
без переменной они пропускаются.
"""

import os

import pytest
from fastapi.testclient import TestClient

from copilot1c import issues as iss
from copilot1c import server
from copilot1c.config import Settings

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")


def test_number_and_meta():
    assert iss.number(7) == "ОБР-0007"
    assert iss.parse_number("ОБР-0012") == 12 and iss.parse_number("обр12") == 12 and iss.parse_number(" 5 ") == 5
    assert iss.parse_number("Номенклатура") is None
    m = iss.meta(("Иванов И.",))
    assert {"value": "new", "label": "новое"} in m["statuses"] and m["analysts"] == ["Иванов И."]
    assert "resolved" not in m["open_statuses"]


def test_clean_validates_values():
    with pytest.raises(iss.IssueError):
        iss._clean({"status": "забыто"})
    with pytest.raises(iss.IssueError):
        iss._clean({"title": "   "})
    with pytest.raises(iss.IssueError):
        iss._clean({"version": 3})  # служебное поле не правится
    assert iss._clean({"objects": ["Справочник.Номенклатура", " ", "Справочник.Номенклатура"],
                       "server": "  "}) == {"objects": ["Справочник.Номенклатура"], "server": None}


@pytest.fixture
def client(tmp_path, monkeypatch):
    from copilot1c.graph.store import GraphStore

    s = Settings(pg_dsn=PG_DSN, project="test-proj", issues_dir="issues", cache_dir=str(tmp_path / ".cache"),
                 yc_api_key="", yc_folder_id="", onec_bin="/nonexistent", analysts=("Иванов И.", "Петрова А."))
    g = GraphStore(settings=s)
    g.init_schema()
    g.conn.execute("TRUNCATE issue_events, issue_attachments, issues, contacts RESTART IDENTITY CASCADE")
    g.conn.commit()
    g.close()
    monkeypatch.chdir(tmp_path)  # вложения — относительно рабочего каталога ядра
    return TestClient(server.create_app(s))


ERROR_1C = ("{Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: Ошибка при вызове метода контекста (Записать): "
            "Поле объекта не обнаружено (КС_Гамма)")


@needs_pg
def test_create_get_list(client):
    meta = client.get("/issues/meta").json()
    assert meta["analysts"] == ["Иванов И.", "Петрова А."]

    contact = client.post("/contacts", json={"name": "Мария Смирнова", "email": "M.Smirnova@Customer.ru",
                                             "organization": "Заказчик", "position": "ведущий бухгалтер"}).json()
    r = client.post("/issues", json={
        "title": "Не проводится реализация после обновления", "description": "После обновления до 11.5.27.75 …",
        "error_text": ERROR_1C, "priority": "high", "objects": ["Документ.РеализацияТоваровУслуг"],
        "initiator_contact_id": contact["id"], "reported_at": "2026-10-05T10:15:00+03:00",
        "infobase": "тестовая", "server": "pfmosvt1ceapp01", "actor": "Иванов И."})
    assert r.status_code == 200, r.text
    issue = r.json()
    assert issue["number"] == "ОБР-0001" and issue["status"] == "new" and issue["status_label"] == "новое"
    assert issue["priority_label"] == "высокий" and issue["category_label"] == "ошибка"
    assert issue["registered_by"] == "Иванов И." and issue["version"] == 1 and issue["already_registered"] is False
    assert issue["initiator"]["email"] == "m.smirnova@customer.ru"  # e-mail хранится в нижнем регистре
    assert [e["type"] for e in issue["events"]] == ["created"] and issue["events"][0]["actor"] == "Иванов И."

    client.post("/issues", json={"title": "Как выгрузить НСИ за 36 месяцев?", "category": "consult", "actor": "Петрова А."})

    rows = client.get("/issues").json()["issues"]
    assert [x["number"] for x in rows] == ["ОБР-0002", "ОБР-0001"]  # новые сверху
    assert rows[1]["initiator_name"] == "Мария Смирнова" and rows[1]["attachments"] == 0
    assert "description" not in rows[1]  # в списке без длинных текстов
    assert [x["id"] for x in client.get("/issues", params={"category": "consult"}).json()["issues"]] == [2]
    assert [x["id"] for x in client.get("/issues", params={"q": "КС_Гамма"}).json()["issues"]] == [1]  # текст ошибки
    assert [x["id"] for x in client.get("/issues", params={"q": "смирнова"}).json()["issues"]] == [1]  # инициатор
    assert [x["id"] for x in client.get("/issues", params={"q": "ОБР-0002"}).json()["issues"]] == [2]  # номер
    assert client.get("/issues/999").status_code == 404


@needs_pg
def test_validation_errors(client):
    assert client.post("/issues", json={"title": "x", "status": "забыто"}).status_code == 422
    assert client.post("/issues", json={"title": ""}).status_code == 422
    assert client.post("/issues", json={"title": "x", "nonsense": 1}).status_code == 422  # лишние поля не принимаются


@needs_pg
def test_same_email_message_is_not_registered_twice(client):
    body = {"title": "Ошибка выгрузки", "source": "email", "source_message_id": "<abc@customer.ru>"}
    first = client.post("/issues", json=body).json()
    again = client.post("/issues", json={**body, "title": "Та же проблема"}).json()
    assert again["id"] == first["id"] and again["already_registered"] is True
    assert len(client.get("/issues").json()["issues"]) == 1


@needs_pg
def test_patch_history_resolve_and_conflict(client):
    issue = client.post("/issues", json={"title": "Пустой отчёт", "actor": "Иванов И."}).json()

    r = client.patch(f"/issues/{issue['id']}", json={
        "version": 1, "actor": "Петрова А.", "comment": "Беру в работу",
        "changes": {"status": "in_progress", "assignee": "Петрова А.", "title": "Пустой отчёт"}})  # тема не менялась
    assert r.status_code == 200, r.text
    cur = r.json()
    assert cur["version"] == 2 and cur["status_label"] == "в работе"
    ev = [(e["type"], e["field"]) for e in cur["events"]]
    assert ev == [("created", None), ("status", "status"), ("field", "assignee"), ("comment", None)]
    assert cur["events"][1]["old_value"] == "new" and cur["events"][1]["new_value"] == "in_progress"

    # второй аналитик правит по старой версии — 409 и свежая карточка
    stale = client.patch(f"/issues/{issue['id']}", json={"version": 1, "changes": {"priority": "low"}})
    assert stale.status_code == 409 and stale.json()["detail"]["current"]["version"] == 2

    done = client.patch(f"/issues/{issue['id']}", json={
        "version": 2, "changes": {"status": "resolved", "root_cause": "нет прав на регистр",
                                  "resolution": "добавлена роль КС_ЧтениеОстатков"}}).json()
    assert done["resolved_at"] and done["version"] == 3
    reopened = client.patch(f"/issues/{issue['id']}", json={"version": 3, "changes": {"status": "in_progress"}}).json()
    assert reopened["resolved_at"] is None  # переоткрытое обращение снова не решено

    same = client.patch(f"/issues/{issue['id']}", json={"version": 4, "changes": {"status": "in_progress"}}).json()
    assert same["version"] == 4  # ничего не изменилось — версия та же, событий нет
    assert client.patch("/issues/999", json={"version": 1, "changes": {}}).status_code == 404


@needs_pg
def test_comments_and_attachments(client, tmp_path):
    issue = client.post("/issues", json={"title": "Ошибка при записи номенклатуры"}).json()
    iid = issue["id"]
    assert client.post(f"/issues/{iid}/comments", json={"text": "Запросили у заказчика скриншот",
                                                        "actor": "Иванов И."}).status_code == 200

    files = [("files", ("скрин ошибки.png", b"\x89PNG-data", "image/png")),
             ("files", ("журнал.log", b"line1\nline2", "text/plain")),
             ("files", ("пустой.txt", b"", "text/plain"))]
    up = client.post(f"/issues/{iid}/attachments", files=files, data={"actor": "Иванов И."}).json()["attachments"]
    assert [a.get("filename") for a in up] == ["скрин ошибки.png", "журнал.log", "пустой.txt"]
    assert up[0]["mime"] == "image/png" and up[0]["already_attached"] is False and "error" in up[2]
    again = client.post(f"/issues/{iid}/attachments", files=[("files", ("копия.png", b"\x89PNG-data", "x"))]).json()
    assert again["attachments"][0]["id"] == up[0]["id"] and again["attachments"][0]["already_attached"] is True
    assert (tmp_path / "issues" / str(iid) / "журнал.log").read_bytes() == b"line1\nline2"

    f = client.get(f"/issues/{iid}/attachments/{up[1]['id']}")
    assert f.status_code == 200 and f.content == b"line1\nline2"
    assert client.get(f"/issues/{iid}/attachments/999").status_code == 404
    assert client.post("/issues/999/attachments", files=[("files", ("a.txt", b"x", "text/plain"))]).status_code == 404

    card = client.get(f"/issues/{iid}").json()
    assert len(card["attachments"]) == 2 and card["attachments"][0]["filename"] == "скрин ошибки.png"
    assert [e["type"] for e in card["events"]] == ["created", "comment", "attachment", "attachment"]
    assert client.get("/issues").json()["issues"][0]["attachments"] == 2


@needs_pg
def test_contacts_upsert_by_email(client):
    a = client.post("/contacts", json={"name": "Мария Смирнова", "email": "m.smirnova@customer.ru"}).json()
    b = client.post("/contacts", json={"name": "Смирнова М.", "email": "M.SMIRNOVA@customer.ru",
                                       "position": "ведущий бухгалтер", "phone": "+7 495 000-00-00"}).json()
    assert b["id"] == a["id"] and b["name"] == "Мария Смирнова"  # имя не перезаписывается
    assert b["position"] == "ведущий бухгалтер"  # пустые поля дополняются
    assert client.post("/contacts", json={"name": ""}).status_code == 422
    assert [c["id"] for c in client.get("/contacts", params={"q": "смирн"}).json()["contacts"]] == [a["id"]]


def test_issues_without_postgres_is_503(monkeypatch):
    from copilot1c.graph import store

    monkeypatch.setattr(store, "try_connect", lambda s=None: None)
    c = TestClient(server.create_app(Settings()))
    assert c.get("/issues").status_code == 503
    assert c.get("/issues/meta").status_code == 200  # справочники не требуют базы


@needs_pg
def test_nul_and_control_chars_are_removed(client):
    # в письмах Outlook (.msg) бывает NUL — PostgreSQL его не принимает, раньше это давало HTTP 500
    r = client.post("/issues", json={"title": "Бланк\x00 заказа", "description": "Дмитрий,\x00\n\tБланк\x07 не грузится",
                                     "objects": ["Документ.Заказ\x00"]})
    assert r.status_code == 200, r.text
    issue = r.json()
    assert issue["title"] == "Бланк заказа" and issue["description"] == "Дмитрий,\n\tБланк не грузится"
    assert issue["objects"] == ["Документ.Заказ"]
    assert client.post(f"/issues/{issue['id']}/comments", json={"text": "ок\x00"}).status_code == 200
    c = client.post("/contacts", json={"name": "Иванов\x00 Иван", "email": "i@pierre-fabre.com\x00",
                                       "position": "бухгалтер\x00"}).json()
    assert c["name"] == "Иванов Иван" and c["email"] == "i@pierre-fabre.com" and c["position"] == "бухгалтер"


def test_unexpected_error_has_text_detail(monkeypatch):
    from copilot1c.graph import store

    def broken(s=None):
        raise RuntimeError("что-то сломалось")

    monkeypatch.setattr(store, "try_connect", broken)
    c = TestClient(server.create_app(Settings()), raise_server_exceptions=False)
    r = c.get("/issues")
    assert r.status_code == 500 and "RuntimeError: что-то сломалось" in r.json()["detail"]
