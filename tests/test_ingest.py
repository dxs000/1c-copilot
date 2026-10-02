import io
import shutil
import zipfile
from datetime import datetime

import pytest
from fixtures.synthetic import THREAD_BODY, make_eml, make_pimi, make_tz
from openpyxl import Workbook

from copilot1c.ingest.attachments import parse_bytes
from copilot1c.ingest.chunking import document_chunks
from copilot1c.ingest.cleaning import clean_email_text, mask_pii, unwrap_safelinks
from copilot1c.ingest.corpus import Corpus
from copilot1c.ingest.document import expand_ranges, guess_doc_type, guess_doc_version
from copilot1c.ingest.docx import parse_docx
from copilot1c.ingest.entities import extract_regex_entities, find_md_objects, is_custom_object
from copilot1c.ingest.msg import message_chunk, parse_eml, people
from copilot1c.ingest.thread import parse_date, split_thread
from copilot1c.models import DocType, EntityKind

# --- переписка ---


def test_split_thread_restores_quoted_messages():
    own, quoted = split_thread(THREAD_BODY)
    assert "Направляем на согласование ДС №10" in own
    assert [q.sender for q in quoted] == ["Иванов Сергей Петрович", "Smith John"]
    ivanov, smith = quoted
    assert ivanov.date == datetime(2026, 9, 22, 12, 24)
    assert smith.date == datetime(2026, 9, 22, 10, 37)
    assert smith.sender_email == "john.smith@customer.com"
    assert "8.3.27.1859" in ivanov.text
    assert "custsrvapp01" in smith.text


def test_clean_email_text():
    own, quoted = split_thread(THREAD_BODY)
    body = clean_email_text(own)
    assert body.startswith("Добрый день")  # баннер шлюза убран
    assert "С уважением" not in body and "+7" not in body  # подпись с телефоном отрезана
    smith = clean_email_text(quoted[1].text)
    assert "Charte" not in smith and "confidential" not in smith
    assert smith.startswith("Уважаемые коллеги")  # табуляция цитаты снята


def test_parse_date_variants():
    assert parse_date("Tuesday, September 22, 2026 at 10:37 AM") == datetime(2026, 9, 22, 10, 37)
    assert parse_date("вторник, 22 сентября 2026 г. 10:37") == datetime(2026, 9, 22, 10, 37)
    assert parse_date("22.09.2026 10:37") == datetime(2026, 9, 22, 10, 37)


def test_gmail_style_quote():
    body = "Ответ выше.\n\nпт, 15 сент. 2026 г. в 14:39, Иван Петров <ivan@x.ru>:\n> Старый текст\n> ещё"
    own, quoted = split_thread(body)
    assert own.strip() == "Ответ выше."
    assert quoted[0].text.strip().startswith("Старый текст")


def test_eml_with_attachments_and_dedup(tmp_path):
    tz = make_tz(tmp_path / "tz.docx")
    eml = make_eml(tmp_path / "letter.eml", [("ТЗ ред2.docx", tz.read_bytes(),
                                              "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
    e = parse_eml(eml)
    assert len(e.messages) == 3 and e.messages[0].origin == "file"
    assert e.attachments[0].filename == "ТЗ ред2.docx"

    # То же письмо ещё раз (как вложение в другой архив) и та же цитата — в корпусе по одному разу
    corpus = Corpus(project="p").add_paths([eml, eml])
    assert len(corpus.messages) == 3
    assert len(corpus.documents) == 1
    assert corpus.documents[0].received[0].startswith("вложение письма «RE: Обновление")
    chunk = message_chunk(corpus.messages[1])
    assert "Кому: Smith John (customer.com); Петрова Анна (integrator.ru)" in chunk.text
    assert "@" not in chunk.text.replace("(integrator.ru)", "").split("\n\n", 1)[0].replace("email@", "")


def test_people_and_masking():
    assert people("A B <a@x.ru> <mailto:a@x.ru> , C D <c@y.com>") == "A B (x.ru); C D (y.com)"
    assert mask_pii("тел. +7 (495) 983-04-12 #6572, a@b.ru") == "тел. <телефон>, <email@b.ru>"
    assert unwrap_safelinks("x https://eur03.safelinks.protection.outlook.com/?url=https%3A%2F%2Fq.ru%2F&data=1 y") \
        == "x https://q.ru/ y"


# --- документы ---


def test_tz_structure(tmp_path):
    d = parse_docx(make_tz(tmp_path / "ТЗ_на_обновление_ред2.docx"))
    assert d.doc_type == DocType.TZ and d.version == "2.0"
    assert d.title.startswith("Техническое задание на обновление")
    titles = [s.title for s in d.sections]
    assert "1. Общие положения / 1.3. Объем работ Исполнителя" in titles
    assert not any("\t1" in t for t in titles)  # оглавление пропущено

    item64 = next(p for p in d.plan_items if p.num == "64")
    assert item64.group.endswith("3. НСИ")
    assert len(item64.procedures) == 2  # две строки одного пункта склеены
    assert "«Номенклатура»" in item64.objects
    assert [p.num for p in d.plan_items] == ["64", "65"]

    assert d.coverage[0].items == ["64", "70", "71", "72"]
    assert d.removed == ["Удалённая формулировка про закрытие месяца."]
    assert "Удалённая" not in d.full_text()
    assert d.comments[0].author == "Рецензент" and "Предопределенные" in d.comments[0].anchor


def test_pimi_autonumbered_headings_and_steps(tmp_path):
    d = parse_docx(make_pimi(tmp_path / "pimi.docx"))
    assert d.doc_type == DocType.PIMI
    titles = [s.path[-1] for s in d.sections if s.path]
    assert titles[:4] == ["1 Общие сведения", "2 Программы испытаний",
                          "2.1 Программа испытаний справочника \"Номенклатура\"",
                          "2.1.1 Регистрация номенклатуры к выгрузке"]
    assert titles[4] == "2.2 Программа испытаний справочника \"Контрагенты\""

    tc1, tc2 = d.test_cases
    assert tc1.num == "1" and len(tc1.steps) == 3
    assert tc1.result == "Не работает"  # худший результат шагов
    assert tc1.section.endswith("2.1.1 Регистрация номенклатуры к выгрузке / Работа с обработкой. "
                                "Выборка отдельных элементов номенклатуры")
    assert "ШтрихкодыНоменклатуры" in tc1.objects
    assert tc2.num == "2" and tc2.result == "Работает"

    chunks = document_chunks(d, "p")
    tc_chunk = next(c for c in chunks if c.extra.get("test_case") == "1")
    assert "Шаг 3. Методика проверки: Проверить ШтрихкодыНоменклатуры" in tc_chunk.text
    assert tc_chunk.attributes()["result"] == "Не работает"


@pytest.mark.skipif(not shutil.which("soffice"), reason="нужен LibreOffice для получения PDF")
def test_pdf_matches_docx(tmp_path):
    from copilot1c.ingest.attachments import convert_with_soffice

    tz = make_tz(tmp_path / "tz.docx")
    pdf = convert_with_soffice(tz.read_bytes(), "tz.docx", "pdf")
    (d,) = parse_bytes(pdf, "ТЗ ред2.pdf", "tz.pdf")
    assert d.doc_type == DocType.TZ
    assert {p.num for p in d.plan_items} == {"64", "65"}
    assert d.coverage[0].items[:2] == ["64", "70"]

    # docx и его PDF-версия в корпусе — один документ; остаётся docx (в нём комментарии и правки)
    (tmp_path / "ТЗ ред2.pdf").write_bytes(pdf)
    corpus = Corpus().add_paths([tmp_path / "ТЗ ред2.pdf", tz])
    assert len(corpus.documents) == 1
    assert corpus.documents[0].filename == "tz.docx" and corpus.documents[0].comments


def test_xlsx_test_matrix():
    wb = Workbook()
    ws = wb.active
    ws.append(["№", "Функция", "Методика проверки", "Критерий успешности", "Результат"])
    ws.append(["Раздел 1. Склад"])
    ws.merge_cells("A2:E2")
    ws.append([1, "Приходный ордер", "Создать ордер", "Ордер проведён", "Работает"])
    ws.append([1, "Приходный ордер", "Распечатать", "Форма открыта", "Работает"])
    buf = io.BytesIO()
    wb.save(buf)
    (d,) = parse_bytes(buf.getvalue(), "ПиМИ склад.xlsx", "x.xlsx")
    assert d.doc_type == DocType.PIMI
    assert len(d.test_cases) == 1 and len(d.test_cases[0].steps) == 2
    assert d.test_cases[0].section.endswith("Раздел 1. Склад")


def test_zip_and_unknown(tmp_path):
    pimi = make_pimi(tmp_path / "p.docx").read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("docs/ПиМИ.docx", pimi)
        z.writestr("readme.bin", b"\x00\x01")
    results = parse_bytes(buf.getvalue(), "pack.zip", "pack.zip")
    kinds = sorted(type(r).__name__ for r in results)
    assert kinds == ["ParsedDocument", "Skipped"]


def test_doc_meta_guessing():
    assert guess_doc_type("ДС10 к Договору 240605_ver 3.docx") == DocType.DS
    assert guess_doc_type("ПРОГРАММА и МЕТОДИКА ИСПЫТАНИЙ") == DocType.PIMI
    assert guess_doc_type("ТЗ_на_обновление_УТ_ред2_Заказчик.docx") == DocType.TZ
    assert guess_doc_version("ТЗ_ред2.docx") == "2" and guess_doc_version("x_ver 3 23092026") == "3"
    assert expand_ranges("1–3, 129, 147-148") == ["1", "2", "3", "129", "147", "148"]


# --- сущности ---


def test_regex_entities():
    text = (THREAD_BODY + " Справочник «Предопределенные значения (КС)», КС_Гамма, Приказы (КС). "
            "ТЗ на обновление «1С:Управление торговлей» с версии 11.5.19.55")
    names = {(e.kind, e.name) for e in extract_regex_entities(text)}
    assert (EntityKind.SOFTWARE_VERSION, "УТ 11.5.27.75") in names
    assert (EntityKind.SOFTWARE_VERSION, "УТ 11.5.19.55") in names  # продукт из полного названия
    assert (EntityKind.SOFTWARE_VERSION, "Платформа 8.3.27.1859") in names
    assert (EntityKind.SERVER, "custsrvapp01") in names
    assert (EntityKind.DOCUMENT, "ДС № 10") in names
    assert (EntityKind.MD_OBJECT, "Предопределенные значения (КС)") in names
    assert (EntityKind.MD_OBJECT, "Приказы (КС)") in names
    assert is_custom_object("КС_Гамма") and not is_custom_object("Номенклатура")


def test_find_md_objects():
    found = find_md_objects('форма отбора для справочника "номенклатура", регистр сведений «Цены номенклатуры», '
                            "ШтрихкодыНоменклатуры, ПиМИ")
    assert found == ["РегистрСведений «Цены номенклатуры»", "Справочник «Номенклатура»", "ШтрихкодыНоменклатуры"]
