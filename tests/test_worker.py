"""Фоновая обработка загруженных материалов (worker.py) на живом PostgreSQL и поддельном Vector Store.

Нужна переменная COPILOT_TEST_PG_DSN (пустая тестовая база); без неё тесты пропускаются.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from fixtures.synthetic import make_eml, make_pimi, make_tz

from copilot1c import server, worker
from copilot1c.config import Settings
from copilot1c.ingest.corpus import Corpus

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")


class FakeIndex:
    """Vector Store с манифестом в памяти: запоминает, что загружалось."""

    def __init__(self, manifest: dict[str, str] | None = None):
        self.manifest = dict(manifest or {})
        self.uploaded: list[str] = []

    def load_manifest(self):
        return dict(self.manifest)

    def add(self, chunks, progress=None):
        for c in chunks:
            if c.chunk_id not in self.manifest:
                self.manifest[c.chunk_id] = f"file-{c.chunk_id}"
                self.uploaded.append(c.chunk_id)
        return {c.chunk_id: self.manifest[c.chunk_id] for c in chunks}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from copilot1c.graph.store import GraphStore

    monkeypatch.chdir(tmp_path)  # как демон: data и .cache — относительно каталога ядра
    data = Path("data")
    (data / "mails").mkdir(parents=True)
    make_eml(data / "mails" / "RE Обновление.eml")
    make_tz(data / "ТЗ ред2.docx")
    s = Settings(pg_dsn=PG_DSN, project="test-proj", ocr_backend="none", cache_dir="", yc_api_key="k",
                 yc_folder_id="f", vector_store_id="vs", onec_bin="/nonexistent")
    g = GraphStore(settings=s)
    g.init_schema()
    for t in ("materials", "chunks", "mentions", "test_cases", "requirements", "requirement_tests"):
        g.conn.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
    g.conn.commit()
    # Индекс как после «index-docs data»: все основные материалы уже загружены
    base = Corpus(project=s.project, settings=s).add_paths([data]).chunks()
    index = FakeIndex({c.chunk_id: f"file-{c.chunk_id}" for c in base})
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

    # в индекс ушли только фрагменты нового документа, основные материалы не перезаливались
    assert len(index.uploaded) == new["report"]["chunks_new"]
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
