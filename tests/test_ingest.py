from docx import Document

from copilot1c.ingest.cleaning import clean_email_body, mask_pii, strip_quoted, unwrap_safelinks
from copilot1c.ingest.docx import docx_chunks, guess_doc_type, guess_doc_version, parse_docx
from copilot1c.ingest.entities import extract_regex_entities, is_custom_object
from copilot1c.models import DocType, EntityKind

EMAIL = """Коллеги, добрый день!

Обновляемся на УТ 11.5.27.75 (LTS), платформа 8.3.27.2342 на сервере pfmosvt1ceapp01.
Подробности в ДС № 10 и ТЗ ред. 2, проверка по ПиМИ. Ссылка:
https://eur01.safelinks.protection.outlook.com/?url=https%3A%2F%2Fexample.ru%2Fdoc&data=1
Тел. +7 (495) 123-45-67, пишите ivanov@example.ru

С уважением,
Иван

Данное сообщение конфиденциально. Если вы не являетесь получателем, удалите его.

From: Петров
Sent: 15 September 2026
Старое письмо, которое не должно попасть в индекс.
"""


def test_clean_email_body():
    body = clean_email_body(EMAIL)
    assert "Старое письмо" not in body
    assert "конфиденциально" not in body
    assert "С уважением" not in body
    assert "https://example.ru/doc" in body
    assert "<телефон>" in body and "123-45-67" not in body
    assert "<email@example.ru>" in body


def test_strip_quoted_variants():
    assert strip_quoted("Новое\n-----Исходное сообщение-----\nСтарое") == "Новое"
    assert strip_quoted("Новое\n> цитата\nещё") == "Новое\nещё"


def test_safelinks_and_pii_keep_domain():
    assert unwrap_safelinks("x https://a.safelinks.protection.outlook.com/?url=https%3A%2F%2Fq.ru y") == "x https://q.ru y"
    assert mask_pii("support@corp.ru", keep_domains=("corp.ru",)) == "support@corp.ru"


def test_regex_entities():
    names = {(e.kind, e.name) for e in extract_regex_entities(EMAIL + " КС_Гамма и Приказы (КС)")}
    assert (EntityKind.SOFTWARE_VERSION, "УТ 11.5.27.75") in names
    assert (EntityKind.SOFTWARE_VERSION, "Платформа 8.3.27.2342") in names
    assert (EntityKind.SERVER, "pfmosvt1ceapp01") in names
    assert (EntityKind.DOCUMENT, "ДС № 10") in names
    assert (EntityKind.DOCUMENT, "ТЗ ред. 2") in names
    assert (EntityKind.MD_OBJECT, "КС_Гамма") in names
    assert (EntityKind.MD_OBJECT, "Приказы (КС)") in names
    assert is_custom_object("КС_Гамма") and is_custom_object("Приказы (КС)")
    assert not is_custom_object("Номенклатура")


def _make_pimi(path):
    doc = Document()
    doc.add_heading("4 Проверка выгрузки НСИ", level=1)
    doc.add_heading("4.1 Номенклатура", level=2)
    doc.add_paragraph("Проверяется перенос справочника Номенклатура из УТ 10.")
    t = doc.add_table(rows=1, cols=5)
    for cell, text in zip(t.rows[0].cells, ["№", "Функция", "Методика проверки", "Критерий успешности",
                                            "Результат"], strict=True):
        cell.text = text
    row = t.add_row().cells
    merged = row[0].merge(row[4])
    merged.text = "Реквизиты номенклатуры"
    for values in (["12", "Артикул", "Открыть карточку", "Артикул и код должны совпадать", "Работает"],
                   ["13", "Штрихкоды", "Проверить ШтрихкодыНоменклатуры", "Штрихкоды перенесены", "Не работает"]):
        for cell, text in zip(t.add_row().cells, values, strict=True):
            cell.text = text
    doc.add_heading("5 Прочее", level=1)
    doc.add_paragraph("Текст раздела 5, реквизит КС_Гамма.")
    doc.save(path)


def test_parse_pimi(tmp_path):
    path = tmp_path / "ПиМИ Выгрузка НСИ ver 3.docx"
    _make_pimi(path)
    parsed = parse_docx(path, known_objects=["Номенклатура"])
    assert [tc.num for tc in parsed.test_cases] == ["12", "13"]
    tc12, tc13 = parsed.test_cases
    assert tc12.section == "4 Проверка выгрузки НСИ / 4.1 Номенклатура / Реквизиты номенклатуры"
    assert tc12.criterion == "Артикул и код должны совпадать"
    assert tc13.result == "Не работает"
    assert "ШтрихкодыНоменклатуры" in tc13.objects
    assert [s.title for s in parsed.sections] == ["4 Проверка выгрузки НСИ / 4.1 Номенклатура", "5 Прочее"]

    chunks = docx_chunks(path, project="p", known_objects=["Номенклатура"])
    tc_chunks = [c for c in chunks if "test_case" in c.extra]
    assert len(tc_chunks) == 2 and all(c.doc_type == DocType.PIMI for c in tc_chunks)
    assert tc_chunks[0].doc_version == "3"
    assert "КС_Гамма" in chunks[1].objects


def test_doc_type_guess():
    assert guess_doc_type("ТЗ ред2 Выгрузка НСИ.docx") == DocType.TZ
    assert guess_doc_type("ДС №10 к договору.docx") == DocType.DS
    assert guess_doc_type("ПиМИ НСИ.docx") == DocType.PIMI
    assert guess_doc_version("ТЗ ред2.docx") == "2"


def test_email_chunks_dedupe_nested():
    from datetime import datetime

    from copilot1c.ingest.msg import ParsedEmail, _thread_id, email_chunks

    inner = ParsedEmail(subject="RE: Обновление УТ 11", sender="Петров", to="Иванов", cc="", date=None,
                        body="Ответ исполнителя", thread_id="Обновление УТ 11", source="a.msg#att0")
    outer = ParsedEmail(subject="FW: RE: Обновление УТ 11", sender="Иванов", to="Сидоров", cc="",
                        date=datetime(2026, 9, 23, 10, 0), body="Пересылаю", thread_id="Обновление УТ 11",
                        source="a.msg", nested=[inner, inner])
    chunks = email_chunks(outer, project="p")
    assert [c.source for c in chunks] == ["a.msg", "a.msg#att0"]
    assert "Дата: 23.09.2026 10:00" in chunks[0].text
    assert _thread_id("FW: RE: Ответ: Обновление УТ 11") == "Обновление УТ 11"
