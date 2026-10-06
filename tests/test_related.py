"""Похожие обращения и связанные тест-кейсы ПиМИ и пункты ТЗ."""

import json
import os

import pytest
from fastapi.testclient import TestClient

from copilot1c import related as rel
from copilot1c import server
from copilot1c.config import Settings

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")
ERROR = "{Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: Поле объекта не обнаружено (КС_Гамма)"
PROJECT = "test-related"


def test_object_names_from_different_sources_match():
    assert rel.obj_key("Документ.РеализацияТоваровУслуг") == rel.obj_key("Документ «Реализация товаров и услуг»")
    assert rel.obj_key("Справочник.НоменклатураКонтрагентов") == rel.obj_key("«Номенклатура контрагентов»")
    assert rel.obj_key("Справочник «Номенклатура»") == rel.obj_key("Справочник.Номенклатура") == "номенклатура"
    assert rel.obj_key("КС_Гамма") == "кс_гамма"
    assert rel.obj_key("Номенклатура") != rel.obj_key("Номенклатура контрагентов")


def test_text_similarity():
    a = rel.stems("Не проводится реализация у отдела продаж после обновления")
    b = rel.stems("Реализация не проводится, ошибка при проведении")
    assert rel.cosine(a, b) > rel.cosine(a, rel.stems("Выгрузка НСИ из УТ 10 по глубине 36 месяцев"))
    assert rel.cosine(set(), b) == 0


@pytest.fixture
def client(tmp_path, monkeypatch):
    from copilot1c.graph.store import GraphStore

    s = Settings(pg_dsn=PG_DSN, project=PROJECT, issues_dir="issues", cache_dir=str(tmp_path / ".cache"),
                 yc_api_key="", yc_folder_id="", onec_bin="/nonexistent")
    g = GraphStore(settings=s)
    g.init_schema()
    g.conn.execute("TRUNCATE issue_events, issue_attachments, issues, contacts RESTART IDENTITY CASCADE")
    for t in ("test_cases", "requirements"):
        g.conn.execute(f"DELETE FROM {t} WHERE project = %s", (PROJECT,))
    pimi = "ПиМИ Выгрузка и загрузка НСИ"
    tcs = [("12", "2.1 Справочник Номенклатура", "Артикул и код должны совпадать",
            [{"method": "Записать номенклатуру", "criterion": "артикул равен коду"}], "Работает",
            ["Справочник «Номенклатура»"]),
           ("31", "4.1 Документы продажи", "Проведение реализации товаров и услуг",
            [{"method": "Провести реализацию", "criterion": "документ проведён"}], "Не работает",
            ["Документ «Реализация товаров и услуг»", "КС_Гамма"]),
           ("40", "5 Отчёты", "Отчёт по остаткам на складах", [], "Работает", [])]
    for num, sec, fn, steps, res, objs in tcs:
        g.conn.execute("INSERT INTO test_cases (project, doc, num, section, function, steps, result, objects, source) "
                       "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'x')", (PROJECT, pimi, num, sec, fn, json.dumps(steps), res, objs))
    for rid, obj, text, objs in [("64", "Номенклатура", "Проверка выгрузки номенклатуры", ["«Номенклатура»"]),
                                 ("90", "Реализация товаров и услуг", "Проведение реализации после обновления",
                                  ["«Реализация товаров и услуг»"])]:
        g.conn.execute("INSERT INTO requirements (project, req_id, doc, grp, object, text, objects) "
                       "VALUES (%s,%s,'ТЗ (ред. 2.0)','3. НСИ',%s,%s,%s)", (PROJECT, rid, obj, text, objs))
    g.conn.commit()
    g.close()
    monkeypatch.chdir(tmp_path)
    return TestClient(server.create_app(s))


@needs_pg
def test_related_for_issue_and_draft(client):
    first = client.post("/issues", json={"title": "Не проводится реализация после обновления", "error_text": ERROR,
                                         "objects": ["Документ.РеализацияТоваровУслуг", "КС_Гамма"]}).json()
    client.post("/issues", json={"title": "Как выгрузить НСИ за 36 месяцев?", "category": "consult"})
    # та же ошибка другими словами — дубль первого
    second = client.post("/issues", json={"title": "Менеджеры не могут провести документ продажи",
                                          "description": f"Текст: {ERROR}"}).json()

    r = client.get(f"/issues/{second['id']}/related").json()
    assert [x["number"] for x in r["issues"]] == ["ОБР-0001"]  # консультация про НСИ не похожа
    dup = r["issues"][0]
    assert any("то же место ошибки" in w for w in dup["why"]) and dup["status_label"] == "новое"

    r = client.get(f"/issues/{first['id']}/related").json()
    assert r["test_cases"][0]["num"] == "31" and any("объекты" in w for w in r["test_cases"][0]["why"])
    assert all(t["num"] != "40" for t in r["test_cases"])  # отчёт по остаткам ни при чём
    assert r["requirements"][0]["num"] == "90"
    assert all(x["id"] != first["id"] for x in r["issues"])  # само обращение не в списке

    draft = client.post("/issues/related", json={"title": "Артикул номенклатуры не совпадает с кодом",
                                                 "objects": ["Справочник.Номенклатура"]}).json()
    assert draft["test_cases"][0]["num"] == "12" and draft["requirements"][0]["num"] == "64"
    # объекты не заполнены, но есть текст ошибки — пункт ТЗ про реализацию всё равно находится
    by_error = client.post("/issues/related", json={"title": "Документ продажи", "error_text": ERROR}).json()
    assert by_error["requirements"][0]["num"] == "90" and by_error["test_cases"][0]["num"] == "31"
    assert client.get("/issues/999/related").status_code == 404
