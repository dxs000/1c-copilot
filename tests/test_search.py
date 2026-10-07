"""База поиска в PostgreSQL (search.py), контуры, удаление материала, очистка базы.

Тесты с базой идут на живом PostgreSQL с pgvector из COPILOT_TEST_PG_DSN; эмбеддинги поддельные.
"""

import os

import pytest
from fastapi.testclient import TestClient
from fixtures.fake_embed import fake_embed
from typer.testing import CliRunner

from copilot1c import cli, server
from copilot1c.config import Settings
from copilot1c.models import Chunk, DocType
from copilot1c.retrieval import smart_search
from copilot1c.search import PgIndex, search_fn

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")


def _chunk(text, title="", doc_type=DocType.DOC, source="data/doc.docx", **extra):
    return Chunk(text=text, title=title, doc_type=doc_type, source=source, project="test-proj",
                 extra={k: str(v) for k, v in extra.items()})


CORPUS = [
    _chunk("Исходная версия: 11.5.19.55. Целевая версия конфигурации УТ 11.5.27.75.", "Состав работ",
           doc_type=DocType.DS, source="data/ДС10.docx"),
    _chunk("Платформа 8.3.27.2342 подходит: минимальная версия для УТ 11.5.27.75 — 8.3.27.1859.",
           "RE: Обновление", doc_type=DocType.EMAIL, source="data/re.eml"),
    _chunk("Проверить, что артикул номенклатуры совпадает с кодом.", "Тест-кейс № 12", doc_type=DocType.PIMI,
           source="data/pimi.docx", test_case="12"),
    _chunk("Проверить загрузку партнеров из УТ 10.", "Тест-кейс № 21", doc_type=DocType.PIMI,
           source="data/pimi.docx", test_case="21"),
    _chunk("Командировочные расходы: билеты учитываются на счете 71.01 до утверждения авансового отчета.",
           "Учет билетов", source="data/uploads/2026-10-07/БП_командировки.docx"),
]


@pytest.fixture
def pg():
    from copilot1c.graph.store import GraphStore

    s = Settings(pg_dsn=PG_DSN, project="test-proj", ocr_backend="none", cache_dir="", onec_bin="/nonexistent")
    g = GraphStore(settings=s)
    g.init_schema()
    for t in ("mentions", "relations", "entities", "chunks", "materials", "contours"):
        g.conn.execute(f"DELETE FROM {t}")
    g.conn.commit()
    yield s, g
    g.conn.rollback()
    g.close()


@needs_pg
def test_add_is_idempotent_and_returns_only_new(pg):
    s, g = pg
    index = PgIndex(g.conn, s, embed=fake_embed)
    assert len(index.add(CORPUS[:3])) == 3
    assert index.add(CORPUS) == [CORPUS[3].chunk_id, CORPUS[4].chunk_id]  # уже записанные пропускаются
    assert index.stats() == {"chunks": 5, "embedded": 5, "active": 5}


@needs_pg
def test_version_number_found_by_words(pg):
    """Промах «from-version» в Vector Store: номер версии теперь находит полнотекстовая часть."""
    s, g = pg
    index = PgIndex(g.conn, s, embed=fake_embed)
    index.add(CORPUS)
    hits = index.search("с какой версии 11.5.19.55 обновляется конфигурация", k=3)
    assert hits[0]["text"].startswith("Исходная версия: 11.5.19.55")
    assert hits[0]["ranks"]["lexical"] == 1
    assert hits[0]["attributes"]["doc_type"] == "ds" and hits[0]["attributes"]["status"] == "active"


@needs_pg
def test_filters_columns_attrs_and_exact_test_case(pg):
    s, g = pg
    index = PgIndex(g.conn, s, embed=fake_embed)
    index.add(CORPUS)
    emails = index.search("версия платформы", filters={"project": "test-proj", "doc_type": "email"}, k=10)
    assert {h["attributes"]["doc_type"] for h in emails} == {"email"}
    # smart_search: «тест-кейс 21» — точным фильтром по attrs, первым
    hits = smart_search(search_fn(index), "что проверяет тест-кейс 21", {"project": "test-proj"}, 5)
    assert hits[0]["attributes"]["test_case"] == "21"
    assert index.search("номенклатура", filters={"project": "другой"}, k=5) == []


@needs_pg
def test_contours_and_superseded_status(pg):
    from copilot1c.contours import ContourRegistry

    s, g = pg
    reg = ContourRegistry(g.conn, s.project)
    bp = reg.create({"kind": "system", "name": "БП 3.0", "aliases": ["Бухгалтерия", "БП3"]})
    trips = reg.create({"kind": "process", "name": "Командировки", "parent_id": bp["id"]})
    index = PgIndex(g.conn, s, embed=fake_embed)
    index.add(CORPUS[:4])
    index.add(CORPUS[4:], contours=[bp["id"], trips["id"]])
    g.conn.commit()

    only_trips = index.search("учет билетов командировки", contours=[trips["id"]], k=10)
    assert [h["chunk_id"] for h in only_trips] == [CORPUS[4].chunk_id]
    assert only_trips[0]["contours"] == [bp["id"], trips["id"]]

    index.set_status([CORPUS[0].chunk_id], "superseded")
    g.conn.commit()
    ids = {h["chunk_id"] for h in index.search("Исходная версия 11.5.19.55", k=10)}
    assert CORPUS[0].chunk_id not in ids  # заменённая редакция вне поиска по умолчанию
    ids = {h["chunk_id"] for h in index.search("Исходная версия 11.5.19.55", k=10, statuses=("active", "superseded"))}
    assert CORPUS[0].chunk_id in ids

    assert [c["name"] for c in reg.match("Проводки в БП3 по билетам")] == ["БП 3.0"]


@needs_pg
def test_contour_registry_validation(pg):
    from copilot1c.contours import ContourError, ContourRegistry

    s, g = pg
    reg = ContourRegistry(g.conn, s.project)
    c = reg.create({"kind": "system", "name": "УТ 11", "aliases": ["УТ11", "ут11", " "]})
    assert c["aliases"] == ["УТ11"] and c["kind_label"] == "система"
    with pytest.raises(ContourError) as e:
        reg.create({"kind": "system", "name": "УТ 11"})
    assert e.value.status == 409
    with pytest.raises(ContourError):
        reg.create({"kind": "отдел", "name": "x"})
    with pytest.raises(ContourError):
        reg.update(c["id"], {"parent_id": c["id"]})
    assert reg.update(c["id"], {"active": False})["active"] is False
    assert reg.list() == [] and len(reg.list(include_inactive=True)) == 1


@needs_pg
def test_delete_material_endpoint_and_reupload(pg, tmp_path, monkeypatch):
    s, g = pg
    monkeypatch.chdir(tmp_path)
    c = TestClient(server.create_app(s))
    up = c.post("/materials", files=[("files", ("заметка.txt", "Учет билетов".encode(), "text/plain"))]).json()
    mid = up["materials"][0]["id"]
    assert c.delete(f"/materials/{mid}").status_code == 409  # ещё в очереди

    g.conn.execute("UPDATE materials SET status = 'done' WHERE id = %s", (mid,))
    g.conn.commit()
    index = PgIndex(g.conn, s, embed=fake_embed)
    index.add(CORPUS[4:], material_id=mid)
    index.add(CORPUS[:1])
    g.conn.execute("INSERT INTO entities (key, kind, name) VALUES ('process:билеты', 'process', 'билеты')")
    g.conn.execute("INSERT INTO mentions VALUES ('process:билеты', %s)", (CORPUS[4].chunk_id,))
    g.conn.commit()

    r = c.delete(f"/materials/{mid}").json()
    assert r["removed_chunks"] == 1 and r["material"]["status"] == "deleted"
    assert r["material"]["status_label"] == "удалён"
    assert index.stats()["chunks"] == 1  # чужие фрагменты на месте
    assert c.delete("/materials/999999").status_code == 404

    again = c.post("/materials", files=[("files", ("заметка.txt", "Учет билетов".encode(), "text/plain"))]).json()
    assert again["materials"][0]["id"] == mid and again["materials"][0]["status"] == "queued"
    assert again["materials"][0]["already_uploaded"] is False


@needs_pg
def test_contours_api(pg):
    s, g = pg
    c = TestClient(server.create_app(s))
    r = c.post("/contours", json={"kind": "system", "name": "БП 3.0", "aliases": ["БП3"]})
    assert r.status_code == 200
    cid = r.json()["id"]
    assert c.post("/contours", json={"kind": "system", "name": "БП 3.0"}).status_code == 409
    assert c.post("/contours", json={"kind": "x", "name": "y"}).status_code == 422
    assert c.patch(f"/contours/{cid}", json={"notes": "Командировки — счёт 71"}).json()["notes"].startswith("Команд")
    assert c.patch("/contours/999999", json={"notes": "x"}).status_code == 404
    body = c.get("/contours").json()
    assert [x["name"] for x in body["contours"]] == ["БП 3.0"] and body["kinds"]["process"] == "процесс"


@needs_pg
def test_reset_knowledge_keeps_issues_and_contours(pg, monkeypatch):
    s, g = pg
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    from copilot1c.contours import ContourRegistry

    ContourRegistry(g.conn, s.project).create({"kind": "system", "name": "УТ 11"})
    PgIndex(g.conn, s, embed=fake_embed).add(CORPUS)
    g.conn.commit()
    assert CliRunner().invoke(cli.app, ["reset-knowledge"]).exit_code == 1  # без --yes ничего не удаляется
    assert PgIndex(g.conn, s).stats()["chunks"] == 5
    g.conn.commit()  # открытая транзакция держала бы блокировку против ALTER TABLE в init_schema
    r = CliRunner().invoke(cli.app, ["reset-knowledge", "--yes"])
    assert r.exit_code == 0, r.output
    g.conn.commit()
    assert PgIndex(g.conn, s).stats()["chunks"] == 0
    assert len(ContourRegistry(g.conn, s.project).list()) == 1


def test_index_docs_without_postgres(monkeypatch, tmp_path):
    from copilot1c.graph import store

    monkeypatch.setattr(store, "try_connect", lambda s=None: None)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(cache_dir=str(tmp_path), ocr_backend="none"))
    (tmp_path / "note.txt").write_text("ДС № 10 на обновление УТ 11.5.27.75", encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["index-docs", str(tmp_path / "note.txt")])
    assert result.exit_code == 1 and "PostgreSQL недоступен" in result.output
