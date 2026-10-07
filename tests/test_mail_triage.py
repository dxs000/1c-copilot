"""Письмо по обращениям (mail_triage.py): обновление существующего, новое обращение или материал для базы.

Сценарий: ОБР зарегистрировано из письма пользователя «Обновление УТ11 0000026095»; через неделю приходит ответ
с хвостом переписки. Живой PostgreSQL из COPILOT_TEST_PG_DSN, без модели.
"""

import os
from email.message import EmailMessage as Mime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from copilot1c import cli, server
from copilot1c.config import Settings
from copilot1c.mail_triage import agent_block, external_refs, triage

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")

FIRST_BODY = ("Добрый день! После обновления УТ11 не проводится реализация товаров, ошибка при проведении: "
              "Поле объекта не обнаружено (КС_Гамма). Прошу помочь, отгрузки стоят.")
REPLY_BODY = "Коллеги, исправление установили на тестовую базу, проверили — реализация проводится. Ждём подтверждения."


def eml(subject, sender, date, msgid, body):
    m = Mime()
    m["Subject"], m["From"], m["Date"], m["Message-ID"] = subject, sender, date, msgid
    m.set_content(body)
    return bytes(m)


FIRST = eml("0000026095 Обновление УТ11", "Петрова Анна <a.petrova@pierre-fabre.com>", "Mon, 28 Sep 2026 09:10:00 +0300",
            "<first-26095@pierre-fabre.com>", FIRST_BODY)
REPLY = eml("RE: 0000026095 Обновление УТ11", "Иванов Петр <p.ivanov@integrator.ru>", "Tue, 06 Oct 2026 17:40:00 +0300",
            "<reply-26095@integrator.ru>",
            REPLY_BODY + "\n\nFrom: Петрова Анна\nSent: Monday, September 28, 2026 9:10 AM\n"
                         "Subject: 0000026095 Обновление УТ11\n\n" + FIRST_BODY)


def test_external_refs():
    assert external_refs("RE: 0000026095 Обновление УТ11") == ["0000026095"]
    assert external_refs("FW: [#4512] не печатается счет", "INC0012345 / SR-77881") == ["4512", "INC0012345", "SR-77881"]
    assert external_refs("Обновление УТ 11.5.27.75 до 8.3.27.2342") == []  # версии — не номера заявок


@pytest.fixture
def env(tmp_path, monkeypatch):
    from copilot1c.graph.store import GraphStore

    monkeypatch.chdir(tmp_path)
    s = Settings(pg_dsn=PG_DSN, project="test-proj", ocr_backend="none", cache_dir="", yc_api_key="", yc_folder_id="",
                 onec_bin="/nonexistent", issues_dir="issues", intent_llm=False, thread_summary_llm=False,
                 intake_llm=False)
    g = GraphStore(settings=s)
    g.init_schema()
    for t in ("letters", "threads", "issue_events", "issue_attachments", "issues", "contacts", "mentions", "chunks"):
        g.conn.execute(f"DELETE FROM {t}")
    g.conn.commit()
    yield s, g, TestClient(server.create_app(s))
    g.conn.rollback()
    g.close()


def _register(c) -> dict:
    """Как «Из письма…» → «Зарегистрировать»: создать обращение и приложить письмо."""
    issue = c.post("/issues", json={"title": "Не проводится реализация после обновления", "description": FIRST_BODY,
                                    "source": "email", "source_message_id": "<first-26095@pierre-fabre.com>"}).json()
    att = c.post(f"/issues/{issue['id']}/attachments", files=[("files", ("0000026095.eml", FIRST, "message/rfc822"))],
                 data={"expand": "true"}).json()
    assert att["attachments"][0]["linked"]["refs"] == ["0000026095"]
    return issue


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_reply_is_update_of_registered_issue(env):
    s, g, c = env
    issue = _register(c)
    res = triage([("RE 0000026095 Обновление УТ11.msg.eml", REPLY)], "просмотри письмо", g.conn, s)
    g.conn.rollback()
    e = res["emails"][0]
    assert e["decision"] == "update" and e["candidates"][0]["id"] == issue["id"]
    assert "переписка этой ветки уже связана с обращением" in e["candidates"][0]["why"]
    assert any(w.startswith("номер заявки в теме: 0000026095") for w in e["candidates"][0]["why"])
    assert e["letters"]["letters_new"] == 1 and e["new_letters"][0]["sender"] == "Иванов Петр"
    assert "исправление установили" in e["update"]["what_changed"] and e["update"]["method"] == "template"
    assert e["candidates"][0]["version"] >= 1  # для PATCH с защитой от одновременной правки
    block = agent_block(res)
    assert f"Относится к ОБР-{issue['id']:04d}" in block and "новых писем 1" in block


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_ticket_number_alone_finds_issue(env):
    """Отдельное письмо без цитат, но с тем же номером заявки в теме."""
    s, g, c = env
    issue = _register(c)
    other = eml("Re: [0000026095] уточнение", "Сидоров Олег <o.sidorov@pierre-fabre.com>",
                "Wed, 07 Oct 2026 10:00:00 +0300", "<x@pf>", "Подскажите сроки, пожалуйста.")
    e = triage([("уточнение.eml", other)], "", g.conn, s)["emails"][0]
    g.conn.rollback()
    assert e["decision"] == "update" and e["candidates"][0]["id"] == issue["id"]


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_new_problem_and_plain_material(env):
    s, g, c = env
    _register(c)
    problem = eml("Не печатается счет-фактура", "Сидоров Олег <o.sidorov@pierre-fabre.com>",
                  "Wed, 07 Oct 2026 10:00:00 +0300", "<p@pf>",
                  "После обновления не формируется печатная форма счета-фактуры, ошибка при вызове метода контекста.")
    info = eml("График отпусков", "HR <hr@pierre-fabre.com>", "Wed, 07 Oct 2026 11:00:00 +0300", "<i@pf>",
               "Направляем график отпусков отдела на ноябрь для сведения.")
    res = triage([("счет.eml", problem), ("график.eml", info)], "", g.conn, s)
    g.conn.rollback()
    assert [e["decision"] for e in res["emails"]] == ["new", "knowledge"]


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_chat_phrase_from_user_gets_triage_not_new_issue(env, monkeypatch):
    from types import SimpleNamespace

    s, g, c = env
    issue = _register(c)
    seen = {}
    monkeypatch.setattr(server, "search_sources", lambda s, q, k=8: [])
    monkeypatch.setattr(server, "run_question", lambda s, q, attached="", search_query=None, task="": seen.update(
        task=task) or SimpleNamespace(answer="ok", steps=1, trace=[]))
    ck = TestClient(server.create_app(s.model_copy(update={"yc_api_key": "k", "yc_folder_id": "f"})))
    q = ("просмотри приложенный документ. При необходимости зарегистрируй новое обращение или обнови из текста "
         "письма работу с уже существующим обращением")
    r = ck.post("/ask/files", data={"question": q},
                files=[("files", ("RE 0000026095 Обновление УТ11.eml", REPLY, "message/rfc822"))]).json()
    assert r["intent"]["primary"] == "issue_mail" and r["intent"]["primary_label"] == "письмо по обращениям"
    assert r["issue_draft"] is None and r["intake"] is None
    assert r["triage"]["emails"][0]["decision"] == "update"
    assert r["triage"]["emails"][0]["candidates"][0]["id"] == issue["id"]
    assert seen["task"].startswith("Аналитик просит разобрать письмо по обращениям")


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_backfill_links_old_attachments(env, monkeypatch):
    """Обращения, заведённые до этого патча: письмо во вложениях есть, связи с веткой нет."""
    s, g, c = env
    issue = c.post("/issues", json={"title": "Старое обращение", "description": FIRST_BODY}).json()
    folder = Path("issues") / str(issue["id"])
    folder.mkdir(parents=True)
    (folder / "old.eml").write_bytes(FIRST)
    g.conn.execute("INSERT INTO issue_attachments (issue_id, filename, path, size, sha256) VALUES (%s, %s, %s, %s, %s)",
                   (issue["id"], "old.eml", f"issues/{issue['id']}/old.eml", len(FIRST), "x"))
    g.conn.commit()
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    r = CliRunner().invoke(cli.app, ["link-issue-emails"])
    assert r.exit_code == 0, r.output
    assert "номера заявок 0000026095" in r.output
    e = triage([("re.eml", REPLY)], "", g.conn, s)["emails"][0]
    g.conn.rollback()
    assert e["decision"] == "update" and e["candidates"][0]["id"] == issue["id"]
    assert c.post("/issues/triage", files=[("files", ("re.eml", REPLY, "message/rfc822"))]).json()["emails"][0][
        "decision"] == "update"


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_each_letter_is_a_history_item(env):
    """Каждое письмо переписки — отдельный пункт истории обращения: автор, роль, время отправки."""
    s, g, c = env
    s2 = s.model_copy(update={"internal_domains": ("pierre-fabre.com",)})
    c = TestClient(server.create_app(s2))
    issue = _register(c)
    reply = c.post(f"/issues/{issue['id']}/attachments",
                   files=[("files", ("re.eml", REPLY, "message/rfc822"))]).json()["attachments"][0]
    assert reply["linked"]["history_letters"] == 1  # первое письмо уже было в истории — добавилось только новое
    events = c.get(f"/issues/{issue['id']}").json()["events"]
    letters = [e for e in events if e["type"] == "letter"]
    assert [(e["actor"], e["new_value"]["role"]) for e in letters] == [("Петрова Анна", "сотрудник"),
                                                                        ("Иванов Петр", "внешний (исполнитель)")]
    assert letters[0]["at"].startswith("2026-09-28T09:10") and letters[1]["at"].startswith("2026-10-06T17:40")
    assert letters[1]["comment"].startswith("Коллеги, исправление установили")
    assert events.index(letters[0]) < [e["type"] for e in events].index("created")  # история — по времени писем
    again = c.post(f"/issues/{issue['id']}/attachments",
                   files=[("files", ("re2.eml", REPLY + b" ", "message/rfc822"))]).json()["attachments"][0]
    assert again["linked"]["history_letters"] == 0


def test_importance_header_leftover_is_removed():
    from copilot1c.ingest.cleaning import clean_email_text

    assert clean_email_text("Importance: High\n\nДобрый день!\nБланк не грузится.", mask=False).startswith("Добрый день!")
