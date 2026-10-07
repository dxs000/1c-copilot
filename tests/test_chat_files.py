"""Файлы к вопросу в чате («+»): текст для агента, лимиты, черновик обращения по письму."""

import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_email_intake import ANALYST_EMAILS, ANALYSTS, DOMAINS, forwarded_as_attachment

from copilot1c import chat_files as cf
from copilot1c import server
from copilot1c.config import Settings


def _settings(**kw):
    return Settings(ocr_backend="none", yc_api_key="k", yc_folder_id="f", onec_bin="/nonexistent",
                    intent_llm=False, internal_domains=DOMAINS, analyst_emails=ANALYST_EMAILS, analysts=ANALYSTS, **kw)


def test_email_with_inner_files_and_text_file():
    items = cf.extract([("fw.eml", forwarded_as_attachment()), ("заметка.txt", "Проверить КС_Гамма".encode())],
                       _settings())
    assert [(i.filename, i.kind) for i in items] == [
        ("fw.eml", "email"), ("скрин ошибки.png (из письма «fw.eml»)", "skipped"),
        ("журнал.log (из письма «fw.eml»)", "document"), ("заметка.txt", "document")]
    mail = items[0].text
    assert "Письмо «Не проводится реализация» — Smirnova Maria <m.smirnova@pierre-fabre.com>, 05.10.2026 10:15" in mail
    assert "Поле объекта не обнаружено" in mail and "Ведущий бухгалтер" not in mail  # подпись отрезана
    assert items[0].inner == ["скрин ошибки.png", "журнал.log"] and "line1" in items[2].text
    block = cf.context_block(items)
    assert block.startswith("### Файл «fw.eml»") and "### Файл «заметка.txt»" in block and "скрин" not in block


def test_limits(monkeypatch):
    monkeypatch.setattr(cf, "PER_FILE_CHARS", 100)
    monkeypatch.setattr(cf, "TOTAL_CHARS", 150)
    items = cf.extract([(f"{i}.txt", ("x" * 300).encode()) for i in range(3)], _settings())
    assert [len(i.text) for i in items] == [100, 50, 0]
    assert "обрезан" in items[0].note and "общим лимитом" in items[1].note and "не вошёл" in items[2].note
    broken = cf.extract([("битое.msg", b"not an ole file")], _settings())[0]
    assert broken.kind == "skipped" and broken.note.startswith("не разобран")


def test_ask_files_passes_context_and_email_issue_draft(monkeypatch):
    seen = {}
    monkeypatch.setattr(server, "search_sources", lambda s, q, k=8: seen.setdefault("search", q) and [])
    monkeypatch.setattr(server, "run_question", lambda s, q, attached="", search_query=None, task="": seen.update(
        q=q, attached=attached, sq=search_query) or SimpleNamespace(answer="Это ошибка КС_Гамма", steps=1, trace=[]))
    c = TestClient(server.create_app(_settings(pg_dsn="postgresql://nobody@127.0.0.1:1/x")))
    r = c.post("/ask/files", data={"question": "Что с этим делать?"},
               files=[("files", ("fw.eml", forwarded_as_attachment(), "message/rfc822"))])
    assert r.status_code == 200, r.text
    body = r.json()
    assert seen["q"] == "Что с этим делать?" and "### Файл «fw.eml»" in seen["attached"]
    assert "Не проводится реализация" in seen["sq"]  # поиск — по вопросу и теме письма
    assert [a["filename"] for a in body["attachments"]][0] == "fw.eml" and body["attachments"][0]["kind"] == "email"
    d = body["issue_draft"]
    assert body["intent"]["is_issue"] and d["title"] == "Не проводится реализация"
    assert d["initiator"]["email"] == "m.smirnova@pierre-fabre.com" and d["source"] == "email"
    assert d["source_message_id"] == "<user-42@pierre-fabre.com>" and d["reported_at"].startswith("2026-10-05")


def test_ask_files_errors():
    c = TestClient(server.create_app(_settings()))
    many = [("files", (f"{i}.txt", b"x", "text/plain")) for i in range(cf.MAX_FILES + 1)]
    assert c.post("/ask/files", data={"question": "Что тут?"}, files=many).status_code == 413
    no_keys = TestClient(server.create_app(Settings(yc_api_key="", yc_folder_id="")))
    assert no_keys.post("/ask/files", data={"question": "Что тут?"},
                        files=[("files", ("a.txt", b"x", "text/plain"))]).status_code == 503


PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_ask_files_knows_registered_email(monkeypatch):
    from copilot1c.graph.store import GraphStore

    s = _settings(pg_dsn=PG_DSN, project="test-chat-files")
    g = GraphStore(settings=s)
    g.init_schema()
    g.conn.execute("DELETE FROM issues WHERE project = 'test-chat-files'")
    g.conn.commit()
    g.close()
    monkeypatch.setattr(server, "search_sources", lambda s, q, k=8: [])
    monkeypatch.setattr(server, "run_question", lambda s, q, attached="", search_query=None, task="":
                        SimpleNamespace(answer="ок", steps=1, trace=[]))
    c = TestClient(server.create_app(s))
    issue = c.post("/issues", json={"title": "Реализация", "source_message_id": "<user-42@pierre-fabre.com>"}).json()
    body = c.post("/ask/files", data={"question": "Не проводится реализация, что делать?"},
                  files=[("files", ("fw.eml", forwarded_as_attachment(), "message/rfc822"))]).json()
    assert body["issue_draft"]["already_registered"]["id"] == issue["id"]
