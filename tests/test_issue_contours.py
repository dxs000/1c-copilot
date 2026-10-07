"""Система и подсистема обращения: стартовый справочник, предложение по тексту и объектам 1С, обучение,
фильтр списка, «передано», вес в похожих. Живой PostgreSQL из COPILOT_TEST_PG_DSN, без модели."""

import os

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from copilot1c import cli, server
from copilot1c.config import Settings
from copilot1c.contours import ContourError, ContourRegistry, suggest

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")

RDP = ("PFMOSVp1CEAPP01 висит служба терминалов. Добрый день! Вот так висит попытка подключения уже полтора часа. "
       "Может, сессия на сервере зависла, просьба завершить все мои сессии.")
SALE = "{Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: Поле объекта не обнаружено (КС_Гамма)"


@pytest.fixture
def env(tmp_path, monkeypatch):
    from copilot1c.contour_catalog import seed
    from copilot1c.graph.store import GraphStore

    monkeypatch.chdir(tmp_path)
    s = Settings(pg_dsn=PG_DSN, project="test-contours", ocr_backend="none", cache_dir="", yc_api_key="",
                 yc_folder_id="", onec_bin="/nonexistent", issues_dir="issues", intent_llm=False, intake_llm=False,
                 thread_summary_llm=False)
    g = GraphStore(settings=s)
    g.init_schema()
    for sql in ("DELETE FROM issue_events WHERE issue_id IN (SELECT id FROM issues WHERE project = 'test-contours')",
                "DELETE FROM issues WHERE project = 'test-contours'",
                "DELETE FROM contours WHERE project = 'test-contours'"):
        g.conn.execute(sql)
    g.conn.commit()
    counts = seed(g.conn, s.project)
    g.conn.commit()
    tree = {c["name"] if c["kind"] == "system" else f"{c['parent']}›{c['name']}": c["id"]
            for c in _tree(g, s)}
    yield s, g, TestClient(server.create_app(s)), tree, counts
    g.conn.rollback()
    g.close()


def _tree(g, s):
    items = ContourRegistry(g.conn, s.project).list()
    names = {c["id"]: c["name"] for c in items}
    return [{**c, "parent": names.get(c["parent_id"])} for c in items]


def test_seed_catalog_is_idempotent_and_allows_same_block_in_two_systems(env):
    from copilot1c.contour_catalog import seed

    s, g, c, tree, counts = env
    assert counts["systems"] == 4 and counts["subsystems"] >= 25
    assert {"УТ 11›Продажи", "БП 3.0›Продажи", "ЗУП 3›Зарплата", "Инфраструктура›Терминальные серверы"} <= set(tree)
    assert seed(g.conn, s.project) == {"systems": 0, "subsystems": 0, "aliases": 0}
    with pytest.raises(ContourError):  # подсистема без системы
        ContourRegistry(g.conn, s.project).create({"kind": "subsystem", "name": "Висящий блок"})


@pytest.mark.parametrize(("text", "expected"), [
    (RDP, ["Инфраструктура", "Инфраструктура › Терминальные серверы"]),
    (f"После обновления УТ11 не проводится реализация: {SALE}", ["УТ 11", "УТ 11 › Продажи"]),
    ("Не формируется авансовый отчет по командировке в БП, билеты не попадают", ["БП 3.0", "БП 3.0 › Банк и касса"]),
    ("ошибка при расчете зарплаты за сентябрь, больничный не считается", ["ЗУП 3", "ЗУП 3 › Зарплата"]),
    ("В УТ не сходятся остатки на складе после перемещения", ["УТ 11", "УТ 11 › Склад и доставка"]),
    ("Не печатается на принтере на 3 этаже", ["Инфраструктура", "Инфраструктура › Рабочие места и печать"]),
    ("Не проводится реализация", []),  # «Продажи» есть и в УТ, и в БП — решает модель или аналитик
])
def test_suggest(env, text, expected):
    s, g, c, tree, counts = env
    assert suggest(g.conn, s.project, text, settings=s)["labels"] == expected


def test_issue_contours_filter_learning_transferred(env):
    s, g, c, tree, counts = env
    infra, rdp = tree["Инфраструктура"], tree["Инфраструктура›Терминальные серверы"]
    sug = c.post("/issues/suggest-contours", json={"title": "PFMOSVp1CEAPP01 висит служба терминалов",
                                                    "description": RDP}).json()
    assert sug["contours"] == [infra, rdp] and sug["method"] == "heuristic"
    a = c.post("/issues", json={"title": "Висит служба терминалов", "description": RDP, "contours": sug["contours"]}).json()
    b = c.post("/issues", json={"title": "Реализация", "error_text": SALE, "objects": ["Документ.ЗаказНаСборку_КС"],
                                "contours": [tree["УТ 11"]]}).json()
    assert [x["id"] for x in c.get("/issues", params={"contour": infra}).json()["issues"]] == [a["id"]]
    assert [x["id"] for x in c.get("/issues", params={"contour": rdp}).json()["issues"]] == [a["id"]]
    row = c.get("/issues", params={"contour": tree["УТ 11"]}).json()["issues"][0]
    assert row["id"] == b["id"] and row["contours"] == [tree["УТ 11"]]

    # не по профилю — «передано» с адресатом
    t = c.patch(f"/issues/{a['id']}", json={"version": a["version"], "changes": {
        "status": "transferred", "transferred_to": "Системные администраторы"}, "comment": "передано"}).json()
    assert t["status_label"] == "передано" and t["transferred_to"] == "Системные администраторы"
    assert "transferred" not in c.get("/issues/meta").json()["open_statuses"]

    # аналитик отнёс обращение к блоку — его объект 1С стал псевдонимом блока
    sales = tree["УТ 11›Продажи"]
    c.patch(f"/issues/{b['id']}", json={"version": b["version"], "changes": {"contours": [tree["УТ 11"], sales]}})
    learned = suggest(g.conn, s.project, "Ошибка в Документ.ЗаказНаСборку_КС при записи", settings=s)
    assert learned["labels"] == ["УТ 11", "УТ 11 › Продажи"] and "объект ЗаказНаСборку_КС" in learned["why"][str(sales)]


def test_related_prefers_same_system(env):
    from copilot1c.related import find_related

    s, g, c, tree, counts = env
    text = "Не формируется отчет, пустой отчет после обновления, ошибка при формировании"
    same = c.post("/issues", json={"title": "Пустой отчет", "description": text,
                                   "contours": [tree["УТ 11"]]}).json()
    other = c.post("/issues", json={"title": "Пустой отчет", "description": text,
                                    "contours": [tree["ЗУП 3"]]}).json()
    found = {x["id"]: x for x in find_related(g.conn, s.project, {"title": "Пустой отчет", "description": text,
                                                                   "contours": [tree["УТ 11"]]})["issues"]}
    assert found[same["id"]]["score"] > found.get(other["id"], {"score": 0})["score"]
    assert "та же система" in found[same["id"]]["why"]


def test_backfill_cli_and_init_db_seed(env, monkeypatch):
    s, g, c, tree, counts = env
    i = c.post("/issues", json={"title": "Не печатается на принтере",
                                "description": "Принтер на 3 этаже не печатает"}).json()
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    dry = CliRunner().invoke(cli.app, ["suggest-issue-contours", "--no-llm"])
    assert dry.exit_code == 0 and "Инфраструктура › Рабочие места и печать" in dry.output
    assert "Ничего не записано" in dry.output
    assert c.get(f"/issues/{i['id']}").json()["contours"] == []
    r = CliRunner().invoke(cli.app, ["suggest-issue-contours", "--apply", "--no-llm"])
    assert r.exit_code == 0 and "Записано: 1" in r.output
    card = c.get(f"/issues/{i['id']}").json()
    assert card["contours"] == [tree["Инфраструктура"], tree["Инфраструктура›Рабочие места и печать"]]
    assert any("Система определена автоматически" in (e.get("comment") or "") for e in card["events"])

    g.conn.execute("DELETE FROM contours WHERE project = %s", (s.project,))
    g.conn.commit()
    out = CliRunner().invoke(cli.app, ["init-db"])
    assert out.exit_code == 0 and "Справочник контуров: систем 4" in out.output


def test_kb_publish_keeps_issue_contours(env):
    s, g, c, tree, counts = env
    i = c.post("/issues", json={"title": "Висит служба терминалов", "description": RDP, "status": "resolved",
                                "resolution": "Сессии завершены администратором",
                                "contours": [tree["Инфраструктура"], tree["Инфраструктура›Терминальные серверы"]]}).json()
    r = c.post(f"/issues/{i['id']}/kb-publish", json={"title": "Зависшие RDP-сессии", "text": "Симптом: висит "
               "подключение к терминальному серверу. Причина: зависшая сессия. Решение: завершить сессии пользователя."})
    assert r.status_code == 200, r.text
    m = g.query("SELECT decision FROM materials WHERE id = %s", (r.json()["material"]["id"],))[0]
    assert m["decision"]["contours"] == [tree["Инфраструктура"], tree["Инфраструктура›Терминальные серверы"]]
