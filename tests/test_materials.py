"""Материалы, загруженные через веб: сохранение файлов и реестр в PostgreSQL.

Тесты с базой идут на живом PostgreSQL из COPILOT_TEST_PG_DSN (пустая тестовая база — таблица materials
очищается); без переменной они пропускаются.
"""

import os
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from copilot1c import materials as m
from copilot1c import server
from copilot1c.config import Settings

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")


def test_safe_filename():
    assert m.safe_filename("RE: Обновление УТ11.msg") == "RE_ Обновление УТ11.msg"
    assert m.safe_filename("../../etc/passwd") == "passwd"
    assert m.safe_filename(r"C:\Users\x\ТЗ ред.2.docx") == "ТЗ ред.2.docx"
    assert m.safe_filename("...") == "файл" and m.safe_filename("") == "файл"
    long = m.safe_filename("я" * 300 + ".pdf")
    assert long.endswith(".pdf") and len(long) == 150


def test_save_upload_dates_and_name_collisions(tmp_path):
    d = date(2026, 10, 5)
    p1 = m.save_upload(tmp_path, "ТЗ.docx", b"1", d)
    p2 = m.save_upload(tmp_path, "ТЗ.docx", b"2", d)
    p3 = m.save_upload(tmp_path, "ТЗ.docx", b"3", d)
    assert [p.name for p in (p1, p2, p3)] == ["ТЗ.docx", "ТЗ (2).docx", "ТЗ (3).docx"]
    assert p1.parent == tmp_path / "2026-10-05" and p2.read_bytes() == b"2"
    assert not list(tmp_path.rglob("*.part"))  # временные файлы не остаются


@pytest.fixture
def pg_settings(tmp_path, monkeypatch):
    from copilot1c.graph.store import GraphStore

    s = Settings(pg_dsn=PG_DSN, project="test-proj", materials_dir="data/uploads", cache_dir=str(tmp_path / ".cache"),
                 yc_api_key="", yc_folder_id="", onec_bin="/nonexistent")
    g = GraphStore(settings=s)
    g.init_schema()
    g.conn.execute("TRUNCATE materials RESTART IDENTITY CASCADE")  # CASCADE: на materials ссылаются обращения
    g.conn.commit()
    g.close()
    monkeypatch.chdir(tmp_path)  # демон работает в каталоге ядра: data/uploads — относительно него
    return s


@needs_pg
def test_upload_list_get_and_duplicates(pg_settings, tmp_path):
    c = TestClient(server.create_app(pg_settings))
    files = [("files", ("RE: письмо.msg", b"msg-bytes", "application/octet-stream")),
             ("files", ("ТЗ.docx", b"docx-bytes", "application/octet-stream")),
             ("files", ("пустой.txt", b"", "text/plain"))]
    r = c.post("/materials", files=files)
    assert r.status_code == 200
    up = r.json()["materials"]
    assert [x.get("status") for x in up] == ["queued", "queued", None]
    assert up[0]["filename"] == "RE_ письмо.msg" and up[0]["status_label"] == "в очереди"
    assert up[0]["already_uploaded"] is False and "sha256" not in up[0]
    assert up[2]["error"].startswith("пустой файл")
    saved = tmp_path / up[1]["path"]
    assert up[1]["path"].startswith("data/uploads/") and saved.read_bytes() == b"docx-bytes"

    # тот же файл под другим именем: не сохраняется второй раз, возвращается прежняя запись
    again = c.post("/materials", files=[("files", ("копия ТЗ.docx", b"docx-bytes", "x"))]).json()["materials"][0]
    assert again["id"] == up[1]["id"] and again["already_uploaded"] is True
    assert len(list((tmp_path / "data" / "uploads").rglob("*"))) == 3  # папка дня + 2 файла

    listed = c.get("/materials").json()["materials"]
    assert [x["id"] for x in listed] == [up[1]["id"], up[0]["id"]]  # новые сверху
    one = c.get(f"/materials/{up[0]['id']}").json()
    assert one["filename"] == "RE_ письмо.msg" and one["size"] == 9 and one["uploaded_at"]
    assert c.get("/materials/999999").status_code == 404


@needs_pg
def test_set_status_stamps_times_and_report(pg_settings):
    from copilot1c.graph.store import GraphStore

    g = GraphStore(settings=pg_settings)
    reg = m.MaterialRegistry(g.conn, pg_settings.project)
    row, _ = m.register_upload(reg, Path("data/uploads"), "a.txt", b"abc", base=Path.cwd())
    reg.set_status(row["id"], "parsing", "разбираю файл")
    mid = reg.get(row["id"])
    assert mid["status"] == "parsing" and mid["started_at"] and mid["finished_at"] is None
    reg.set_status(row["id"], "done", None, {"chunks_new": 3})
    end = reg.get(row["id"])
    assert end["finished_at"] and end["report"] == {"chunks_new": 3}
    reg.set_status(row["id"], "error", "ошибка AI Studio")  # отчёт не затирается при report=None
    assert reg.get(row["id"])["report"] == {"chunks_new": 3}
    g.close()


def test_materials_without_postgres_is_503(monkeypatch):
    from copilot1c.graph import store

    monkeypatch.setattr(store, "try_connect", lambda s=None: None)
    c = TestClient(server.create_app(Settings()))
    assert c.get("/materials").status_code == 503
    assert c.post("/materials", files=[("files", ("a.txt", b"x", "text/plain"))]).status_code == 503
