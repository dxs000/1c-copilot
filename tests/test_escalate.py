"""Пакет для эксперта (Claude) через оператора: состав архива, ПДн, исходные файлы, обращение, рекомендация агента."""

import io
import json
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_email_intake import forwarded_as_attachment

from copilot1c import escalate as esc
from copilot1c import server
from copilot1c.agent.tools import ToolContext, available_tools, make_handlers
from copilot1c.config import Settings

ERROR = "{Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: Поле объекта не обнаружено (КС_Гамма)"


def _settings(tmp_path, **kw):
    return Settings(cache_dir=str(tmp_path / ".cache"), ocr_backend="none", project="ut11-update",
                    analysts=("Иванов И.",), yc_api_key="", yc_folder_id="", onec_bin="/nonexistent", **kw)


def _zip(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {n: z.read(n) for n in z.namelist()}


def test_package_contents_and_no_personal_data(tmp_path):
    inp = esc.EscalationInput(
        question="Почему после обновления не проводится реализация? Иванов уже проверял права.",
        expert_question="Как перенести реквизит КС_Гамма в расширение без потери данных?",
        reason="В базе проекта и в интернете нет описания переноса реквизита для УТ 11.5.27.75",
        answer="Вероятно, реквизит КС_Гамма не перенесён в расширение. Сообщила m.smirnova@pierre-fabre.com.",
        sources=[{"n": 1, "label": "ПиМИ «Выгрузка НСИ», тест-кейс № 31", "text": "Проведение реализации — Не работает"}],
        web_sources=[{"title": "ИТС: расширения", "url": "https://its.1c.ru/db/1", "read": True}],
        tools=["search_docs", "web_search"],
        attachments=[("fw.eml", forwarded_as_attachment())])
    data, manifest = esc.build(inp, _settings(tmp_path))
    files = _zip(data)
    assert set(files) == {"PROMPT.md", "MANIFEST.json", "materials/01_project_fragments.md", "materials/02_internet.md",
                          "materials/04_attachments_text.md", "materials/05_local_agent.md"}  # исходников нет
    prompt = files["PROMPT.md"].decode()
    for part in ("Ты — эксперт по платформе", "ut11-update", "## Исходный вопрос аналитика",
                 "## Почему вопрос передан эксперту", "## Вопрос эксперту\n\nКак перенести реквизит КС_Гамма",
                 "`materials/01_project_fragments.md` — фрагменты базы проекта: 1", "## Требования к ответу",
                 "поиск в интернете — ссылок: 1"):
        assert part in prompt, part
    everything = b"\n".join(files.values()).decode()
    assert "Иванов" not in everything and "m.smirnova" not in everything and "аналитик уже проверял" in prompt.lower()
    att = files["materials/04_attachments_text.md"].decode()
    assert "Поле объекта не обнаружено" in att
    for person in ("Smirnova", "Maria", "Petrova", "Petrov Pavel", "Иванов Иван"):  # авторы цепочки письма
        assert person not in everything, person
    assert manifest["raw_attachments"] is False and manifest["size"] == len(data)
    assert json.loads(files["MANIFEST.json"])["files"][0]["path"] == "PROMPT.md"


def test_raw_files_only_when_allowed(tmp_path):
    inp = esc.EscalationInput(question="Что в логе?", attachments=[("журнал.log", b"line1"), ("журнал.log", b"line2")],
                              include_raw=True)
    files = _zip(esc.build(inp, _settings(tmp_path))[0])
    assert files["attachments/журнал.log"] == b"line1" and files["attachments/_журнал.log"] == b"line2"


def test_store_load_and_name(tmp_path):
    data, m = esc.build(esc.EscalationInput(question="Вопрос?"), _settings(tmp_path))
    root = tmp_path / "esc"
    path = esc.store(root, data, m)
    assert esc.load(root, m["id"])[0] == path and esc.load(root, "../../etc") is None
    assert esc.package_name(m).startswith("Claude — вопрос ") and esc.package_name(m).endswith(".zip")


def test_agent_recommends_escalation(tmp_path):
    ctx = ToolContext(_settings(tmp_path), Path("x"), None)
    assert "prepare_escalation" in {t["function"]["name"] for t in available_tools(ctx)}
    r = make_handlers(ctx)["prepare_escalation"]("нет данных", "Как перенести реквизит?")
    assert r["ok"] and ctx.escalation == {"reason": "нет данных", "expert_question": "Как перенести реквизит?"}


def test_api_create_and_download(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    c = TestClient(server.create_app(_settings(tmp_path)))
    payload = {"question": "Почему не проводится реализация?", "answer": "Не знаю", "include_raw": True}
    r = c.post("/escalations", data={"payload": json.dumps(payload)},
               files=[("files", ("журнал.log", b"line1", "text/plain"))])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["filename"].startswith("Claude — вопрос") and out["raw_attachments"] is True
    assert any(f["path"] == "attachments/журнал.log" for f in out["files"])
    d = c.get(f"/escalations/{out['id']}")
    assert d.status_code == 200 and d.headers["content-type"] == "application/zip" and "PROMPT.md" in _zip(d.content)
    assert c.get("/escalations/000000000000").status_code == 404
    assert c.post("/escalations", data={"payload": "{}"}).status_code == 422  # ни вопроса, ни обращения
    assert c.post("/escalations", data={"payload": "не json"}).status_code == 422


PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_package_from_issue(tmp_path, monkeypatch):
    from copilot1c.graph.store import GraphStore

    s = _settings(tmp_path, pg_dsn=PG_DSN, issues_dir="issues")
    g = GraphStore(settings=s)
    g.init_schema()
    g.conn.execute("TRUNCATE issue_events, issue_attachments, issues, contacts RESTART IDENTITY CASCADE")
    g.conn.commit()
    g.close()
    monkeypatch.chdir(tmp_path)
    c = TestClient(server.create_app(s))
    contact = c.post("/contacts", json={"name": "Смирнова Мария", "email": "m.smirnova@pierre-fabre.com"}).json()
    issue = c.post("/issues", json={"title": "Не проводится реализация", "error_text": ERROR,
                                    "initiator_contact_id": contact["id"],
                                    "description": "Мария Смирнова пишет: не проводится"}).json()
    c.post(f"/issues/{issue['id']}/comments", json={"text": "Проверили права — в порядке", "actor": "Иванов И."})
    c.post(f"/issues/{issue['id']}/attachments", files=[("files", ("журнал.log", b"line1", "text/plain"))])
    out = c.post("/escalations", data={"payload": json.dumps({"issue_id": issue["id"]})}).json()
    files = _zip(c.get(f"/escalations/{out['id']}").content)
    md = files["materials/03_issue.md"].decode()
    assert "# Обращение ОБР-0001" in md and ERROR in md and "Проверили права" in md
    assert "Смирнова" not in md and "Мария" not in md and "Пользователь пишет" in md
    assert "ОБР-0001: Не проводится реализация" in files["PROMPT.md"].decode()
    assert "журнал.log" in files["materials/04_attachments_text.md"].decode()
    assert out["filename"].startswith("Claude — ОБР-0001")
