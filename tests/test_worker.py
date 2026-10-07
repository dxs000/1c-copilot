"""Фоновая обработка загруженных материалов (worker.py) на живом PostgreSQL с поддельными эмбеддингами.

Нужна переменная COPILOT_TEST_PG_DSN (пустая тестовая база); без неё тесты пропускаются.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fixtures.fake_embed import fake_embed
from fixtures.synthetic import make_eml, make_pimi, make_tz

from copilot1c import server, worker
from copilot1c.config import Settings
from copilot1c.ingest.corpus import Corpus
from copilot1c.search import PgIndex

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")


class FakeIndex(PgIndex):
    """База поиска в PostgreSQL с поддельными эмбеддингами; запоминает, какие фрагменты записывались."""

    def __init__(self, conn, s):
        super().__init__(conn, s, embed=fake_embed)
        self.uploaded: list[str] = []

    def add(self, chunks, **kw):
        new = super().add(chunks, **kw)
        self.uploaded += new
        return new


@pytest.fixture
def env(tmp_path, monkeypatch):
    from copilot1c.graph.store import GraphStore

    monkeypatch.chdir(tmp_path)  # как демон: data и .cache — относительно каталога ядра
    data = Path("data")
    (data / "mails").mkdir(parents=True)
    make_eml(data / "mails" / "RE Обновление.eml")
    make_tz(data / "ТЗ ред2.docx")
    s = Settings(pg_dsn=PG_DSN, project="test-proj", ocr_backend="none", cache_dir="", yc_api_key="k",
                 yc_folder_id="f", onec_bin="/nonexistent")
    g = GraphStore(settings=s)
    g.init_schema()
    for t in ("mentions", "relations", "materials", "chunks", "test_cases", "requirements", "requirement_tests"):
        g.conn.execute(f"DELETE FROM {t}")
    g.conn.commit()
    # База как после «index-docs data»: все основные материалы уже записаны
    base = Corpus(project=s.project, settings=s).add_paths([data]).chunks()
    index = FakeIndex(g.conn, s)
    index.add(base)
    index.uploaded = []
    yield s, g, index, tmp_path
    g.close()


def _upload(s, files):
    c = TestClient(server.create_app(s))  # без фонового потока: обработку вызываем сами
    return c, c.post("/materials", files=[("files", (n, b, "application/octet-stream")) for n, b in files]).json()


def _run(s, g, index):
    batch = worker.claim(g.conn, s.project)
    worker.process_batch(batch, g.conn, s, index=index)
    return batch


def test_new_document_duplicate_email_and_unreadable_file(env, tmp_path):
    s, g, index, root = env
    pimi = make_pimi(tmp_path / "src-ПиМИ.docx").read_bytes()
    dup_mail = (root / "data" / "mails" / "RE Обновление.eml").read_bytes()
    c, up = _upload(s, [("ПиМИ НСИ.docx", pimi), ("копия письма.eml", dup_mail), ("обработка.epf", b"\x00\x01binary")])
    assert [m["status"] for m in up["materials"]] == ["queued"] * 3

    _run(s, g, index)
    got = {m["filename"]: m for m in c.get("/materials").json()["materials"]}

    new = got["ПиМИ НСИ.docx"]
    assert new["status"] == "done" and new["status_label"] == "готово" and new["finished_at"]
    assert new["report"]["chunks_new"] > 0 and new["report"]["documents"][0]["test_cases"] > 0
    assert new["detail"].startswith("добавлено фрагментов")

    dup = got["копия письма.eml"]
    assert dup["status"] == "duplicate" and "уже есть в базе" in dup["detail"]
    assert dup["report"]["emails"] >= 1 and dup["report"]["chunks_new"] == 0

    bad = got["обработка.epf"]
    assert bad["status"] == "error" and "неподдерживаемый тип .epf" in bad["detail"]

    # в базу ушли только фрагменты нового документа, основные материалы не перезаписывались
    assert len(index.uploaded) == new["report"]["chunks_new"]
    owned = g.query("SELECT count(*) AS n FROM chunks WHERE material_id = %s", (new["id"],))[0]["n"]
    assert owned == new["report"]["chunks_new"]  # по material_id материал можно убрать из базы
    n = g.query("SELECT count(*) AS n FROM test_cases WHERE project = 'test-proj'")[0]["n"]
    assert n == new["report"]["documents"][0]["test_cases"]
    pg_chunks = g.query("SELECT count(*) AS n FROM chunks WHERE source LIKE 'data/uploads/%%'")[0]["n"]
    assert pg_chunks == new["report"]["chunks"]


def test_same_document_in_another_format_is_reported_where(env, tmp_path):
    """ТЗ уже в базе; его копия под другим именем — «уже есть» с указанием, где лежит."""
    s, g, index, root = env
    c, _ = _upload(s, [("ТЗ копия.docx", (root / "data" / "ТЗ ред2.docx").read_bytes())])
    _run(s, g, index)
    m = c.get("/materials").json()["materials"][0]
    assert m["status"] == "duplicate" and "ТЗ ред2.docx" in m["detail"] and index.uploaded == []


def test_second_batch_keeps_earlier_uploads_canonical(env, tmp_path):
    """Повторная обработка не меняет фрагменты прежних загрузок (порядок корпуса стабилен)."""
    s, g, index, root = env
    c, _ = _upload(s, [("ПиМИ.docx", make_pimi(tmp_path / "p.docx").read_bytes())])
    _run(s, g, index)
    first = list(index.uploaded)
    c.post("/materials", files=[("files", ("заметка.txt", "Совещание 05.10: переносим испытания НСИ".encode(), "x"))])
    _run(s, g, index)
    assert index.uploaded[: len(first)] == first and len(index.uploaded) == len(first) + 1


def test_recover_returns_interrupted_to_queue(env):
    s, g, index, root = env
    c, up = _upload(s, [("a.txt", "Текст заметки о проекте".encode())])
    worker.claim(g.conn, s.project)  # «упали» посреди разбора
    assert worker.recover(g.conn, s.project) == 1
    m = c.get(f"/materials/{up['materials'][0]['id']}").json()
    assert m["status"] == "queued" and "перезапуском" in m["detail"]


def test_missing_file_is_error(env):
    s, g, index, root = env
    c, up = _upload(s, [("b.txt", "Ещё одна заметка".encode())])
    (root / up["materials"][0]["path"]).unlink()
    _run(s, g, index)
    m = c.get("/materials").json()["materials"][0]
    assert m["status"] == "error" and "файл не найден" in m["detail"]


def test_background_worker_processes_queue(env, monkeypatch):
    """Настоящий поток: загрузка → через пару секунд статус «готово»."""
    import time

    s, g, index, root = env
    monkeypatch.setattr(worker, "process_batch",
                        lambda batch, conn, st, index=None, _orig=worker.process_batch: _orig(batch, conn, st, index=env[2]))
    with TestClient(server.create_app(s, start_worker=True)) as c:
        c.post("/materials", files=[("files", ("заметка.txt", "Решение: обновляемся на 11.5.27.75".encode(), "x"))])
        for _ in range(40):
            m = c.get("/materials").json()["materials"][0]
            if m["status"] not in ("queued", "parsing", "indexing", "graph"):
                break
            time.sleep(0.25)
        assert m["status"] == "done", m
        assert c.get("/health").json()["checks"]["materials_worker"]["ok"] is True


def test_indexing_detail_is_per_file(env, tmp_path):
    """Во время индексации у каждого файла своя подпись: сколько новых фрагментов именно из него."""
    s, g, index, root = env
    seen = {}

    class Spy(FakeIndex):
        def add(self, chunks, **kw):
            for r in g.query("SELECT filename, detail FROM materials WHERE project = 'test-proj'"):
                seen.setdefault(r["filename"], r["detail"])
            return super().add(chunks, **kw)

    spy = Spy(g.conn, s)
    _upload(s, [("ПиМИ.docx", make_pimi(tmp_path / "p.docx").read_bytes()),
                ("копия.eml", (root / "data" / "mails" / "RE Обновление.eml").read_bytes())])
    _run(s, g, spy)
    assert seen["ПиМИ.docx"].startswith("записываю в базу поиска новые фрагменты: ")
    assert seen["копия.eml"].startswith("новых фрагментов в файле нет")
