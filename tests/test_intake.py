"""«Входящие» (intake.py): разбор принесённого, приём по решению аналитика, документы и редакции.

Сценарий из жизни: аналитику пришло письмо «RE: Учет билетов» с тремя документами по командировкам в БП, и те же
документы он приложил отдельно. Живой PostgreSQL из COPILOT_TEST_PG_DSN, эмбеддинги поддельные, без модели.
"""

import io
import json
import os
from email.message import EmailMessage as Mime
from pathlib import Path

import pytest
from docx import Document
from fastapi.testclient import TestClient
from fixtures.fake_embed import fake_embed

from copilot1c import server, worker
from copilot1c.config import Settings
from copilot1c.intake import accept, agent_block, analyze
from copilot1c.search import PgIndex

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")


def docx(title: str, *paras: str) -> bytes:
    d = Document()
    d.add_heading(title, 0)
    for p in paras:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


AS_IS = docx("Бизнес-процесс AS-IS / TO-BE: командировки и учет билетов в 1С:Бухгалтерия",
             "AS-IS: авиабилеты списываются на счет 26 сразу при покупке.",
             "TO-BE: билеты учитываются на счете 71.01 до утверждения авансового отчета.")
REQS = docx("Бизнес-требования, правила и ограничения: командировки",
            "БТ-1. Билет относится на расходы только после утверждения авансового отчета.",
            "БТ-2. Возврат билета отражается сторно.")
REQS_V2 = docx("Бизнес-требования, правила и ограничения: командировки (ред. 2)",
               "БТ-1. Билет относится на расходы только после утверждения авансового отчета.",
               "БТ-2. Возврат билета отражается сторно.",
               "БТ-3. Сбор агентства учитывается отдельно на счете 91.02.")


def letter(*attachments) -> bytes:
    m = Mime()
    m["Subject"] = "RE: Учет билетов"
    m["From"] = "SOKOLOV Dmitry <d.sokolov@pierre-fabre.com>"
    m["Date"] = "Thu, 24 Sep 2026 11:20:00 +0300"
    m["Message-ID"] = "<tickets-0924@pierre-fabre.com>"
    m.set_content("Коллеги, во вложении описание процесса командировок и требования по учету билетов в БП. "
                  "Прошу посмотреть комментарии по проводкам.")
    for name, data in attachments:
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return bytes(m)


@pytest.fixture
def env(tmp_path, monkeypatch):
    from copilot1c.contours import ContourRegistry
    from copilot1c.graph.store import GraphStore

    monkeypatch.chdir(tmp_path)
    Path("data").mkdir()
    s = Settings(pg_dsn=PG_DSN, project="test-proj", ocr_backend="none", cache_dir="", yc_api_key="", yc_folder_id="",
                 onec_bin="/nonexistent", intent_llm=False, thread_summary_llm=False, intake_llm=False)
    g = GraphStore(settings=s)
    g.init_schema()
    for t in ("document_versions", "documents", "letters", "threads", "mentions", "relations", "materials", "chunks",
              "contours"):
        g.conn.execute(f"DELETE FROM {t}")
    g.conn.commit()
    bp = ContourRegistry(g.conn, s.project).create({"kind": "system", "name": "БП 3.0",
                                                     "aliases": ["1С:Бухгалтерия", "БП"]})
    g.conn.commit()
    yield s, g, bp["id"]
    g.conn.rollback()
    g.close()


def _process(s, g):
    batch = worker.claim(g.conn, s.project)
    worker.process_batch(batch, g.conn, s, index=PgIndex(g.conn, s, embed=fake_embed))
    return batch


FILES = [("BP_AS-IS_To-Be_Командировки_1С.docx", AS_IS), ("Бизнес-требования, правила и ограничения.docx", REQS),
         ("RE Учет билетов.eml", None)]


def _files():
    mail = letter(FILES[0][:2], FILES[1][:2])
    return [FILES[0], FILES[1], (FILES[2][0], mail)]


def test_analyze_tells_what_where_and_duplicates(env):
    s, g, bp = env
    res = analyze(_files(), g.conn, s)
    g.conn.rollback()
    a, r, m = res["items"]
    assert (a["kind"], a["doc_kind"], a["action"]) == ("document", "process", "add")
    assert r["doc_kind"] == "requirements" and r["doc_kind_label"] == "бизнес-требования"
    assert a["contours"] == [bp] and r["contours"] == []  # «1С:Бухгалтерия» — псевдоним контура БП 3.0
    assert m["kind"] == "email" and m["email"]["letters_new"] == 1 and m["action"] == "add"
    assert {x["same_as"] for x in m["inner"]} == {FILES[0][0], FILES[1][0]}  # вложения = отдельно приложенные
    block = agent_block(res)
    assert "описание процесса (AS-IS / TO-BE)" in block and "вложения, приложенные и отдельно" in block
    assert "Предложение: добавить в базу" in block


def test_accept_process_documents_contours_and_letters(env):
    s, g, bp = env
    files = _files()
    decisions = [{"filename": n, "action": "add", "contours": [bp]} for n, _ in files]
    decisions[1]["new_contours"] = [{"kind": "process", "name": "Командировки"}]
    out = accept(files, decisions, g.conn, s, base=Path.cwd())
    assert [m["filename"] for m in out["materials"]][-1] == "RE Учет билетов.eml"  # письма — после документов
    trips = out["contours_created"][0]["id"]
    _process(s, g)
    rows = {r["filename"]: r for r in g.query("SELECT filename, status, detail, report FROM materials")}
    assert rows[FILES[0][0]]["status"] == "done" and rows[FILES[1][0]]["status"] == "done"
    mail = rows["RE Учет билетов.eml"]
    assert mail["status"] == "done" and mail["report"]["letters_new"] == 1, mail
    docs = g.query("SELECT title, kind, contours FROM documents ORDER BY id")
    assert [d["kind"] for d in docs] == ["process", "requirements"]
    assert docs[1]["contours"] == [bp, trips]
    hits = PgIndex(g.conn, s, embed=fake_embed).search("билеты счет 71.01", contours=[trips], k=5)
    assert hits and all(trips in h["contours"] for h in hits)
    assert all(h["attributes"].get("document_id") for h in hits if h["attributes"]["doc_type"] != "email")
    thread = g.query("SELECT contours FROM threads")[0]
    assert set(thread["contours"]) == {bp}

    # тот же документ под другим именем — «то же содержание», предложение «не добавлять»
    again = analyze([("копия требований.docx", REQS)], g.conn, s)["items"][0]
    g.conn.rollback()
    assert again["relation"]["type"] == "same_content" and again["action"] == "skip"


def test_new_version_supersedes_previous(env):
    s, g, bp = env
    accept([("БТ.docx", REQS)], [{"filename": "БТ.docx", "action": "add", "contours": [bp]}], g.conn, s, base=Path.cwd())
    _process(s, g)
    doc_id = g.query("SELECT id FROM documents")[0]["id"]

    item = analyze([("БТ ред2.docx", REQS_V2)], g.conn, s)["items"][0]
    g.conn.rollback()
    assert item["relation"]["type"] == "new_version" and item["relation"]["document_id"] == doc_id
    assert item["action"] == "new_version"

    accept([("БТ ред2.docx", REQS_V2)], [{"filename": "БТ ред2.docx", "action": "new_version", "document_id": doc_id,
                                         "contours": [bp]}], g.conn, s, base=Path.cwd())
    _process(s, g)
    m = g.query("SELECT detail, report FROM materials WHERE filename = 'БТ ред2.docx'")[0]
    assert "новая редакция" in m["detail"] and m["report"]["versions"][0]["superseded_versions"] == 1
    versions = g.query("SELECT status FROM document_versions WHERE document_id = %s ORDER BY id", (doc_id,))
    assert [v["status"] for v in versions] == ["superseded", "current"]
    index = PgIndex(g.conn, s, embed=fake_embed)
    current = index.search("требования билеты сбор агентства возврат", k=20)
    assert {h["attributes"]["version_id"] for h in current if h["attributes"].get("version_id")} == \
        {str(g.query("SELECT current_version_id FROM documents")[0]["current_version_id"])}
    old = index.search("требования билеты возврат", k=20, statuses=("superseded",))
    assert old  # прежняя редакция доступна для вопросов «что поменялось»


def test_archive_then_add(env):
    s, g, bp = env
    out = accept([("скан.pdf", b"%PDF-1.4 not really")], [{"filename": "скан.pdf", "action": "archive"}], g.conn, s,
                 base=Path.cwd())
    assert out["materials"][0]["status"] == "archived" and out["materials"][0]["status_label"] == "сохранён без индексации"
    assert worker.claim(g.conn, s.project) == []
    out = accept([("скан.pdf", b"%PDF-1.4 not really")], [{"filename": "скан.pdf", "action": "add"}], g.conn, s,
                 base=Path.cwd())
    assert out["materials"][0]["status"] == "queued"
    skip = accept([("x.txt", b"x")], [{"filename": "x.txt", "action": "skip"}], g.conn, s, base=Path.cwd())
    assert skip["materials"] == [{"filename": "x.txt", "skipped": True}]


def test_api_intake_and_chat_intake_intent(env, monkeypatch):
    from types import SimpleNamespace

    s, g, bp = env
    c = TestClient(server.create_app(s.model_copy(update={"yc_api_key": "k", "yc_folder_id": "f"})))
    files = [("files", (n, d, "application/octet-stream")) for n, d in _files()]
    res = c.post("/intake/analyze", files=files).json()
    assert len(res["items"]) == 3 and res["actions"]["new_version"] and res["contours"][0]["id"] == bp
    acc = c.post("/intake/accept", data={"decisions": json.dumps([{"filename": FILES[0][0], "action": "skip"}])},
                 files=files).json()
    assert acc["materials"][0] == {"filename": FILES[0][0], "skipped": True} and len(acc["materials"]) == 3
    assert c.post("/intake/accept", data={"decisions": "{"}, files=files).status_code == 422

    seen = {}
    monkeypatch.setattr(server, "search_sources", lambda s, q, k=8: [])
    monkeypatch.setattr(server, "run_question", lambda s, q, attached="", search_query=None, task="": seen.update(
        task=task) or SimpleNamespace(answer="разбор", steps=1, trace=[]))
    r = c.post("/ask/files", data={"question": "Просмотреть предложенные документы при необходимости добавить в "
                                               "хранилище"}, files=files).json()
    assert r["intent"]["primary"] == "intake" and r["intent"]["primary_label"] == "принять в базу"
    assert r["issue_draft"] is None and len(r["intake"]["items"]) == 3
    assert seen["task"].startswith("Аналитик просит разобрать") and "Разбор приложенного" in seen["task"]
    assert c.get("/documents").json()["kinds"]["process"].startswith("описание процесса")


def test_delete_material_restores_previous_version_and_forgets_letters(env):
    s, g, bp = env
    accept([("БТ.docx", REQS), ("RE.eml", letter())], [], g.conn, s, base=Path.cwd())
    _process(s, g)
    doc_id = g.query("SELECT id FROM documents")[0]["id"]
    accept([("БТ ред2.docx", REQS_V2)], [{"filename": "БТ ред2.docx", "action": "new_version", "document_id": doc_id}],
           g.conn, s, base=Path.cwd())
    _process(s, g)
    ids = {r["filename"]: r["id"] for r in g.query("SELECT id, filename FROM materials")}
    c = TestClient(server.create_app(s))

    r = c.delete(f"/materials/{ids['БТ ред2.docx']}").json()
    assert r["removed_versions"] == 1 and r["removed_documents"] == 0
    v = g.query("SELECT status FROM document_versions WHERE document_id = %s", (doc_id,))
    assert [x["status"] for x in v] == ["current"]  # прежняя редакция снова текущая
    assert g.query("SELECT count(*) AS n FROM chunks WHERE status = 'superseded'")[0]["n"] == 0

    r = c.delete(f"/materials/{ids['RE.eml']}").json()
    assert r["removed_letters"] == 1 and r["removed_threads"] == 1
    assert g.query("SELECT count(*) AS n FROM letters")[0]["n"] == 0
    # письмо снова — снова новое (а не «уже известно» по забытой записи)
    item = analyze([("RE.eml", letter())], g.conn, s)["items"][0]
    g.conn.rollback()
    assert item["email"]["letters_new"] == 1


def test_retry_after_error(env):
    s, g, bp = env
    out = accept([("БТ.docx", REQS)], [], g.conn, s, base=Path.cwd())
    mid = out["materials"][0]["id"]
    c = TestClient(server.create_app(s))
    assert c.post(f"/materials/{mid}/retry").status_code == 409  # в очереди — повторять нечего
    g.conn.execute("UPDATE materials SET status = 'error', detail = 'нет ключей' WHERE id = %s", (mid,))
    g.conn.commit()
    r = c.post(f"/materials/{mid}/retry").json()
    assert r["status"] == "queued" and r["detail"] == "повтор после ошибки"
    assert c.post("/materials/999999/retry").status_code == 404
    item = analyze([("БТ.docx", REQS)], g.conn, s)["items"][0]
    g.conn.rollback()
    assert item["about"].startswith("БТ-1. Билет относится на расходы")


def test_intake_decided_by_analyst_words_not_email_text(env, monkeypatch):
    """Письмо полно жалоб и вопросов («не работает», «ошибка», «?») — тип всё равно «принять в базу»,
    и модель не вызывается: решают слова аналитика."""
    from types import SimpleNamespace

    from copilot1c import intent

    s, g, bp = env
    noisy = Mime()
    noisy["Subject"], noisy["From"] = "RE: Учет билетов", "SOKOLOV Dmitry <d.sokolov@pierre-fabre.com>"
    noisy.set_content("Срочно! Не работает загрузка билетов, ошибка при загрузке DBF, не проводится авансовый отчет. "
                      "Пользователи жалуются, отчет пустой. Почему так? Подготовьте описание TO-BE?")
    monkeypatch.setattr(intent, "refine_with_llm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM")))
    monkeypatch.setattr(server, "search_sources", lambda s, q, k=8: [])
    monkeypatch.setattr(server, "run_question", lambda s, q, attached="", search_query=None, task="":
                        SimpleNamespace(answer="ok", steps=1, trace=[]))
    c = TestClient(server.create_app(s.model_copy(update={"yc_api_key": "k", "yc_folder_id": "f",
                                                          "intent_llm": True})))
    r = c.post("/ask/files", data={"question": "принять в базу"},
               files=[("files", ("RE.eml", bytes(noisy), "message/rfc822"))]).json()
    assert r["intent"]["primary"] == "intake" and r["intake"]["items"][0]["kind"] == "email"
    assert r["issue_draft"] is None
