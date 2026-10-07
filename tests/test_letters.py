"""Письма и ветки (letters.py): известное и новое в каждом следующем письме ветки, ответы внутри цитат,
смена темы, слияние веток, пересылка вложением. Живой PostgreSQL из COPILOT_TEST_PG_DSN."""

import os
from datetime import datetime, timedelta, timezone

import pytest
from fixtures.fake_embed import fake_embed

from copilot1c.config import Settings
from copilot1c.ingest.msg import EmailMessage, ParsedEmail
from copilot1c.letters import LetterStore, added_lines, agent_view, containment, summarize
from copilot1c.search import PgIndex

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
MSK = timezone(timedelta(hours=3))
PARIS = timezone(timedelta(hours=2))

A_BODY = ("Коллеги, добрый день. Прошу проверить проводки по учету авиабилетов: билеты сейчас списываются на "
          "счет 26 сразу при покупке, а должны висеть на 71.01 до утверждения авансового отчета.")
B_BODY = "Проверили на копии базы: проводки AS-IS действительно на 26 счете. Готовим описание TO-BE до пятницы."
C_BODY = "Описание TO-BE приложено. Просьба согласовать до 30.09, после согласования начнем доработку."


def _m(sender, email, date, body, subject="Учет билетов", origin="file", msgid=None, source="f.msg"):
    return EmailMessage(subject=subject, sender=sender, sender_email=email, to="", cc="", date=date, body=body,
                        source=source, origin=origin, message_id=msgid)


def _file(*msgs, nested=()):
    return ParsedEmail(messages=list(msgs), nested=list(nested), source=msgs[0].source)


A = _m("SOKOLOV Dmitry", "d.sokolov@pierre-fabre.com", datetime(2026, 9, 20, 10, 15, tzinfo=MSK), A_BODY, msgid="<a@pf>")
B_DATE = datetime(2026, 9, 22, 9, 40, tzinfo=MSK)
C_DATE = datetime(2026, 9, 24, 16, 5, tzinfo=MSK)


def _quoted(m, body=None, shift_hours=0, subject=None):
    """Как письмо выглядит в цитате: без Message-ID, время в поясе верхнего письма, текст может быть обрезан."""
    return _m(m.sender, "", m.date + timedelta(hours=shift_hours) if m.date else None, body or m.body,
              subject=subject or f"RE: {m.subject}", origin="quoted", source=m.source + "#quote")


@pytest.fixture
def store():
    from copilot1c.graph.store import GraphStore

    s = Settings(pg_dsn=PG_DSN, project="test-proj", cache_dir="", onec_bin="/nonexistent")
    g = GraphStore(settings=s)
    g.init_schema()
    for t in ("letters", "threads", "mentions", "chunks"):
        g.conn.execute(f"DELETE FROM {t}")
    g.conn.commit()
    yield LetterStore(g.conn, s.project), s, g
    g.conn.rollback()
    g.close()


def test_containment_and_inline_lines():
    assert containment(A_BODY[:120], A_BODY) > 0.9  # обрезанная (даже посреди слова) цитата входит в оригинал
    assert containment(B_BODY, A_BODY) < 0.2
    new = A_BODY + "\n>> ОТВЕТ КС: на 26 счет списывается по ошибке настройки, исправим\nСпасибо"
    assert added_lines(new, A_BODY) == [">> ОТВЕТ КС: на 26 счет списывается по ошибке настройки, исправим"]


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_thread_grows_one_new_letter_per_file(store):
    st, s, g = store
    r1 = st.ingest(_file(A))[0]
    assert r1.thread_new and [x.new for x in r1.letters] == [True]

    # Ответ B: в цитате A — без Message-ID, время прочитано в другом поясе (+1 ч), хвост обрезан
    b = _m("Иванов Петр", "p.ivanov@integrator.ru", B_DATE, B_BODY, subject="RE: Учет билетов", msgid="<b@int>")
    r2 = st.ingest(_file(b, _quoted(A, body=A_BODY[:140], shift_hours=1)))[0]
    assert r2.thread_id == r1.thread_id and not r2.thread_new
    assert [x.new for x in r2.letters] == [True, False]

    # Ответ C со сменой темы: цитаты B и A; новое — только C
    c = _m("SOKOLOV Dmitry", "d.sokolov@pierre-fabre.com", C_DATE, C_BODY, subject="RE: Учет билетов — проводки TO-BE",
           msgid="<c@pf>")
    r3 = st.ingest(_file(c, _quoted(b, subject="RE: Учет билетов"), _quoted(A)))[0]
    assert r3.thread_id == r1.thread_id and len(r3.new_letters) == 1 and r3.new_letters[0].body == C_BODY

    t = st.thread(r1.thread_id)
    assert [x["sender"] for x in t["letters"]] == ["SOKOLOV Dmitry", "Иванов Петр", "SOKOLOV Dmitry"]
    by_body = {x["body"]: x for x in t["letters"]}
    assert by_body[C_BODY]["parent_id"] == by_body[B_BODY]["id"]  # C — ответ на B
    assert by_body[B_BODY]["parent_id"] == by_body[A_BODY]["id"]
    assert t["first_at"] == A.date and t["last_at"] == C_DATE

    # тот же файл второй раз — ничего нового
    again = st.ingest(_file(c, _quoted(b), _quoted(A)))[0]
    assert again.new_letters == [] and again.thread_id == r1.thread_id


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_inline_reply_inside_quote_goes_to_author(store):
    st, s, g = store
    st.ingest(_file(A))
    d = _m("Иванов Петр", "p.ivanov@integrator.ru", B_DATE, "Ответы ниже красным.", subject="RE: Учет билетов",
           msgid="<d@int>")
    quoted_a = _quoted(A, body=A_BODY + "\nОТВЕТ: списание на 26 счет — ошибка настройки вида операции, исправим")
    r = st.ingest(_file(d, quoted_a))[0]
    top, old = r.letters
    assert top.new and not old.new and old.inline_notes == [
        "ОТВЕТ: списание на 26 счет — ошибка настройки вида операции, исправим"]
    assert "ошибка настройки вида операции" in top.chunk.text  # ответ внутри цитаты — в фрагменте автора
    row = st.thread(r.thread_id)["letters"][-1]
    assert row["body"] == "Ответы ниже красным." and "ошибка настройки" in row["inline_notes"]


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_fork_and_merge_of_threads(store):
    st, s, g = store
    ta = st.ingest(_file(A))[0].thread_id
    # Отдельная ветка с другой темой и другим участником
    p = _m("Петрова Анна", "a.petrova@pierre-fabre.com", datetime(2026, 9, 21, 12, 0, tzinfo=PARIS),
           "Авансовые отчеты по командировкам: нужен отчет по неутвержденным билетам.", subject="Авансовые отчеты",
           msgid="<p@pf>")
    tp = st.ingest(_file(p))[0].thread_id
    assert tp != ta
    # Письмо, которое цитирует обе (пересылка с объединением обсуждений) — ветки сливаются
    m = _m("Иванов Петр", "p.ivanov@integrator.ru", C_DATE, "Объединяю обсуждения: это одна задача.",
           subject="FW: Учет билетов", msgid="<m@int>")
    r = st.ingest(_file(m, _quoted(p, subject="Авансовые отчеты"), _quoted(A)))[0]
    assert r.thread_id == min(ta, tp) and r.merged == [max(ta, tp)]
    assert st.thread(max(ta, tp)) is None and len(st.thread(r.thread_id)["letters"]) == 3

    # Развилка: два ответа на одно письмо — оба новые, в той же ветке, parent одинаковый
    f1 = _m("Иванов Петр", "p.ivanov@integrator.ru", B_DATE, B_BODY, subject="RE: Учет билетов", msgid="<f1@int>")
    f2 = _m("Петрова Анна", "a.petrova@pierre-fabre.com", B_DATE + timedelta(minutes=30),
            "Со своей стороны подтверждаю: на 26 счет попадать не должно.", subject="RE: Учет билетов", msgid="<f2@pf>")
    r1 = st.ingest(_file(f1, _quoted(A)))[0]
    r2 = st.ingest(_file(f2, _quoted(A)))[0]
    assert r1.thread_id == r2.thread_id == r.thread_id
    letters = {x["message_id"]: x for x in st.thread(r.thread_id)["letters"]}
    assert letters["<f1@int>"]["parent_id"] == letters["<f2@pf>"]["parent_id"] == letters["<a@pf>"]["id"]


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_known_by_quote_then_arrives_as_attachment(store):
    st, s, g = store
    b = _m("Иванов Петр", "p.ivanov@integrator.ru", B_DATE, B_BODY, subject="RE: Учет билетов", msgid="<b@int>")
    # Сначала B известен только по цитате в ответе C
    c = _m("SOKOLOV Dmitry", "d.sokolov@pierre-fabre.com", C_DATE, C_BODY, subject="RE: Учет билетов", msgid="<c@pf>")
    st.ingest(_file(c, _quoted(b, body=B_BODY[:70], shift_hours=-1)))
    # Потом пересылка, где B — письмо-вложение (с Message-ID и полным текстом)
    fw = _m("Петрова Анна", "a.petrova@pierre-fabre.com", C_DATE + timedelta(hours=1), "Пересылаю для сведения.",
            subject="FW: Учет билетов", msgid="<fw@pf>")
    r = st.ingest(_file(fw, nested=[_file(b)]))
    assert [x.new for x in r[0].letters] == [True] and r[1].letters[0].upgraded and not r[1].letters[0].new
    row = next(x for x in st.thread(r[1].thread_id)["letters"] if x["sender"] == "Иванов Петр")
    assert row["origin"] == "file" and row["message_id"] == "<b@int>" and row["body"] == B_BODY


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_check_is_dry_run_and_agent_view(store):
    st, s, g = store
    st.ingest(_file(A))
    tid = st.list()[0]["id"]
    summarize(st, tid, s, use_llm=False)
    b = _m("Иванов Петр", "p.ivanov@integrator.ru", B_DATE, B_BODY, subject="RE: Учет билетов", msgid="<b@int>")
    res = st.check(_file(b, _quoted(A)))
    assert [x.new for x in res[0].letters] == [True, False] and res[0].thread_id == tid
    assert len(st.thread(tid)["letters"]) == 1  # пробный прогон ничего не записал

    view = agent_view(res)
    assert f"Ветка «Учет билетов» № {tid}: писем в файле 2, новых 1" in view
    assert "Сводка ветки по известным письмам" in view and "Уже известны (в базе): SOKOLOV Dmitry 20.09.2026" in view
    assert B_BODY in view and view.count(A_BODY[:60]) == 1  # старое письмо — только в сводке, не целиком второй раз


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_summary_chunk_is_replaced(store):
    st, s, g = store
    index = PgIndex(g.conn, s, embed=fake_embed)
    r = st.ingest(_file(A))[0]
    index.add([x.chunk for x in r.new_letters])
    first = summarize(st, r.thread_id, s, use_llm=False, index=index)
    assert first["method"] == "template" and first["text"].startswith("Суть: Коллеги")
    b = _m("Иванов Петр", "p.ivanov@integrator.ru", B_DATE, B_BODY, subject="RE: Учет билетов", msgid="<b@int>")
    st.ingest(_file(b, _quoted(A)))
    second = summarize(st, r.thread_id, s, use_llm=False, index=index)
    assert "Последнее письмо — Иванов Петр" in second["text"]
    rows = g.query("SELECT status, attrs->>'kind' AS kind FROM chunks WHERE source = %s ORDER BY indexed_at",
                   (f"thread:{r.thread_id}",))
    assert [x["status"] for x in rows] == ["superseded", "active"] and rows[0]["kind"] == "thread_summary"
    hits = index.search("сводка ветки учет билетов", k=3)
    assert any("Сводка ветки" in h["text"] for h in hits)


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_real_eml_chain_with_timezone_shift(store, tmp_path):
    """Настоящий разбор .eml: письмо Иванова пришло раньше файлом (пояс +02:00), потом — в цитате ответа Петровой,
    где время записано без пояса и прочитано в +03:00. Известно; новое — только письмо Петровой."""
    from email.message import EmailMessage as Mime

    from fixtures.synthetic import make_eml

    from copilot1c.ingest.msg import parse_eml

    earlier = Mime()
    earlier["Subject"] = "Re: Обновление конфигурации 1С УТ11 до версии 11.5.27.75"
    earlier["From"] = "Иванов Сергей Петрович <s.ivanov@integrator.ru>"
    earlier["Date"] = "Tue, 22 Sep 2026 12:24:10 +0200"
    earlier["Message-ID"] = "<ivanov-0922@integrator.ru>"
    earlier.set_content("Добрый день! Так как для УТ 11.5.27.75 минимальная версия платформы 8.3.27.1859, то "
                        "8.3.27.2342 подходит.\n\nС уважением,\nСергей Иванов\n")
    st, s, g = store
    r0 = st.ingest(parse_eml(data=bytes(earlier), source="ivanov.eml"))[0]
    assert [x.new for x in r0.letters] == [True]

    pe = parse_eml(make_eml(tmp_path / "RE.eml"), source="RE.eml")
    check = st.check(pe)[0]
    assert [(x.sender.split()[0], x.new) for x in check.letters] == [("Петрова", True), ("Иванов", False),
                                                                     ("Smith", True)]
    r = st.ingest(pe)[0]
    assert r.thread_id == r0.thread_id and len(r.new_letters) == 2
    assert st.ingest(pe)[0].new_letters == []


def _eml(sender, date, msgid, body, subject="RE: Учет билетов"):
    from email.message import EmailMessage as Mime

    m = Mime()
    m["Subject"], m["From"], m["Date"], m["Message-ID"] = subject, sender, date, msgid
    m.set_content(body)
    return bytes(m)


FIRST = _eml("SOKOLOV Dmitry <d.sokolov@pierre-fabre.com>", "Sun, 20 Sep 2026 10:15:00 +0300", "<a@pf>", A_BODY,
             subject="Учет билетов")
REPLY = _eml("Иванов Петр <p.ivanov@integrator.ru>", "Tue, 22 Sep 2026 09:40:00 +0300", "<b@int>",
             B_BODY + "\n\nFrom: SOKOLOV Dmitry\nSent: Sunday, September 20, 2026 10:15 AM\nSubject: Учет билетов\n\n"
             + A_BODY)


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_api_check_threads_patch_summary(store):
    from fastapi.testclient import TestClient

    from copilot1c import server
    from copilot1c.ingest.msg import parse_eml

    st, s, g = store
    st.ingest(parse_eml(data=FIRST, source="first.eml"))
    g.conn.commit()
    c = TestClient(server.create_app(s.model_copy(update={"thread_summary_llm": False})))

    r = c.post("/letters/check", files=[("files", ("RE.eml", REPLY, "message/rfc822")),
                                        ("files", ("note.txt", b"x", "text/plain"))]).json()["files"]
    chain = r[0]["chains"][0]
    assert (chain["new"], chain["known"], chain["thread_new"]) == (1, 1, False)
    assert "новых 1" in r[0]["agent_view"] and "error" in r[1]
    assert len(c.get("/threads").json()["threads"]) == 1  # пробный прогон ничего не записал

    tid = chain["thread_id"]
    assert c.get(f"/threads/{tid}").json()["letters"][0]["sender"] == "SOKOLOV Dmitry"
    assert c.patch(f"/threads/{tid}", json={"title": "Учет авиабилетов в БП"}).json()["title"] == "Учет авиабилетов в БП"
    assert c.patch(f"/threads/{tid}", json={"issue_id": 999999}).status_code == 422
    assert c.get("/threads", params={"q": "авиабилет"}).json()["threads"][0]["id"] == tid
    summ = c.post(f"/threads/{tid}/summary").json()
    assert summ["method"] == "template" and summ["text"].startswith("Суть:")
    assert c.get("/threads/999999").status_code == 404


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_chat_attachment_gets_delta_and_duplicates_skipped(store):
    from email.message import EmailMessage as Mime

    from copilot1c.chat_files import context_block, extract
    from copilot1c.ingest.msg import parse_eml

    st, s, g = store
    st.ingest(parse_eml(data=FIRST, source="first.eml"))
    summarize(st, st.list()[0]["id"], s, use_llm=False)
    g.conn.commit()

    items = extract([("RE Учет билетов.eml", REPLY)], s, conn=g.conn)
    text = context_block(items)
    assert "Сводка ветки по известным письмам" in text and B_BODY in text
    assert text.count("Прошу проверить проводки") == 1  # старое письмо — один раз (в сводке), не весь хвост
    assert items[0].note == "известные письма ветки — сводкой"

    # Вложение письма, приложенное ещё и отдельным файлом, второй раз не разбирается
    m = Mime()
    m["Subject"], m["From"] = "Учет билетов", "a@b.ru"
    m.set_content("См. вложение")
    m.add_attachment(b"Bilety 71.01", maintype="text", subtype="plain", filename="BP.txt")
    items = extract([("BP.txt", b"Bilety 71.01"), ("fw.eml", bytes(m))], s)
    skipped = [a for a in items if a.kind == "skipped"]
    assert len(skipped) == 1 and skipped[0].note == "то же, что приложенный файл «BP.txt»"


@pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN")
def test_agent_get_thread(store):
    from pathlib import Path

    from copilot1c.agent import tools as agent
    from copilot1c.ingest.msg import parse_eml

    st, s, g = store
    st.ingest(parse_eml(data=FIRST, source="first.eml"))
    st.ingest(parse_eml(data=REPLY, source="re.eml"))
    g.conn.commit()

    class Store:
        conn = g.conn

    handlers = agent.make_handlers(agent.ToolContext(s, Path("x"), Store()))
    out = handlers["get_thread"]("билетов")
    assert out["писем"] == 2 and [x["от"] for x in out["письма"]] == ["SOKOLOV Dmitry", "Иванов Петр"]
    assert out["источник"].startswith("ветка переписки «Учет билетов»")
    assert handlers["get_thread"]("нет такой темы") == {"result": "ветка не найдена"}
    assert "get_thread" in {t["function"]["name"] for t in agent.available_tools(agent.ToolContext(s, Path("x"), Store()))}
