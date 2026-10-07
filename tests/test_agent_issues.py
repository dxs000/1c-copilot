"""Агент чата видит базу обращений: номер в вопросе, поиск, «возможный дубль» в первом сообщении."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from copilot1c.agent import issues_tools as it
from copilot1c.agent import tools as agent
from copilot1c.config import Settings

ERROR = "{Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: Поле объекта не обнаружено (КС_Гамма)"
PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")
PROJECT = "test-agent-issues"


def test_mentioned_numbers():
    assert it.mentioned_numbers("Что с ОБР-0012 и обр 7? И обращение № 3, и ещё ОБР-12") == [12, 7, 3]
    assert it.mentioned_numbers("обращения 2026 года") == [2026]  # число после «обращения» — тоже номер
    assert it.mentioned_numbers("Почему не 11.6?") == []


@pytest.fixture
def store():
    from copilot1c.graph.store import GraphStore
    from copilot1c.issues import IssueRegistry

    s = Settings(pg_dsn=PG_DSN, project=PROJECT, onec_bin="/nonexistent", yc_api_key="", yc_folder_id="")
    g = GraphStore(settings=s)
    g.init_schema()
    g.conn.execute("TRUNCATE issue_events, issue_attachments, issues, contacts RESTART IDENTITY CASCADE")
    g.conn.commit()
    reg = IssueRegistry(g.conn, PROJECT)
    c = reg.upsert_contact("Смирнова Мария", "m.smirnova@pierre-fabre.com", "АО «Пьер Фабр»", phone="+7 495 123-45-67")
    reg.create({"title": "Не проводится реализация после обновления", "error_text": ERROR, "status": "in_progress",
                "objects": ["Документ.РеализацияТоваровУслуг", "КС_Гамма"], "initiator_contact_id": c["id"],
                "root_cause": "Реквизит КС_Гамма не перенесён в расширение"}, "Иванов И.")
    reg.create({"title": "Бланк заказа не грузится в 1С", "description": "Бланк xls не загружается", "status": "closed",
                "resolution": "Пересохранили в xlsx"}, "Иванов И.")
    reg.add_comment(1, "Запросили у заказчика скриншот", "Петрова А.")
    yield s, g
    g.close()


@needs_pg
def test_get_issue_card(store):
    s, g = store
    c = it.get_issue(g.conn, PROJECT, "ОБР-0001")
    assert c["номер"] == "ОБР-0001" and c["статус"] == "в работе" and c["текст_ошибки"] == ERROR
    assert c["инициатор"] == "Смирнова Мария · АО «Пьер Фабр»" and c["причина"].startswith("Реквизит")
    assert "Петрова А.: Запросили у заказчика скриншот" in c["комментарии"][0]
    assert "m.smirnova" not in json.dumps(c, ensure_ascii=False) and "123-45" not in json.dumps(c, ensure_ascii=False)
    assert it.get_issue(g.conn, PROJECT, 2)["решение"] == "Пересохранили в xlsx"
    assert "нет" in it.get_issue(g.conn, PROJECT, "99")["error"]
    assert "не понял" in it.get_issue(g.conn, PROJECT, "abc")["error"]


@needs_pg
def test_search_issues(store):
    s, g = store
    by_error = it.search_issues(g.conn, PROJECT, "Менеджеры не могут провести реализацию: " + ERROR)
    assert by_error[0]["номер"] == "ОБР-0001" and any("место ошибки" in w for w in by_error[0]["почему"])
    by_person = it.search_issues(g.conn, PROJECT, "Смирнова")
    assert [x["номер"] for x in by_person] == ["ОБР-0001"]
    assert it.search_issues(g.conn, PROJECT, "бланк", open_only=True) == []  # закрыто — не в открытых
    assert it.search_issues(g.conn, PROJECT, "бланк")[0]["есть_решение"] is True


@needs_pg
def test_issue_context_mentions_and_duplicates(store):
    s, g = store
    block, listed = it.issue_context(g.conn, PROJECT, "Какой статус у ОБР-0002?")
    assert block.startswith("Обращение ОБР-0002, упомянутое в вопросе") and "Пересохранили в xlsx" in block
    assert listed == [{"number": "ОБР-0002", "id": 2, "title": "Бланк заказа не грузится в 1С", "status": "закрыто",
                       "kind": "mentioned"}]
    block, listed = it.issue_context(g.conn, PROJECT, f"У менеджеров не проводится документ продажи: {ERROR}")
    assert "возможные дубли" in block and "ОБР-0001" in block and listed[0]["kind"] == "similar"
    assert it.issue_context(g.conn, PROJECT, "Почему обновляемся на 11.5.27.75, а не на 11.6?") == ("", [])
    assert "нет в базе обращений" in it.issue_context(g.conn, PROJECT, "Что с ОБР-0099?")[0]


class OneShot:
    def __init__(self):
        self.requests = []

    def create(self, **kw):
        self.requests.append(kw)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=None,
                                                                                content="Это ОБР-0001, в работе."))])


@needs_pg
def test_agent_gets_issues_before_first_call(store, monkeypatch):
    s, g = store
    chat = OneShot()
    monkeypatch.setattr(agent, "client", lambda st: SimpleNamespace(chat=SimpleNamespace(completions=chat)))
    monkeypatch.setattr(agent, "_search", lambda ctx, q, f, k: [])
    ctx = agent.ToolContext(s, Path("x"), g)
    r = agent.run_agent(f"Снова ошибка при проведении реализации: {ERROR}", ctx)
    first = chat.requests[0]["messages"][1]["content"]
    assert "Похожие зарегистрированные обращения — возможные дубли" in first and "ОБР-0001" in first
    assert r.issues and r.issues[0]["number"] == "ОБР-0001"
    names = {t["function"]["name"] for t in chat.requests[0]["tools"]}
    assert {"get_issue", "search_issues"} <= names
    without_db = agent.available_tools(agent.ToolContext(s, Path("x")))
    assert "get_issue" not in {t["function"]["name"] for t in without_db}
    h = agent.make_handlers(ctx)
    assert h["get_issue"]("1")["номер"] == "ОБР-0001" and h["search_issues"]("бланк")[0]["номер"] == "ОБР-0002"
