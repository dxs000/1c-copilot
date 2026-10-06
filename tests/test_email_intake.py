"""Письмо → черновик обращения: инициатор, подпись, дата, тема, файлы, защита от повторной регистрации.

Аналитики и пользователи — сотрудники одной организации (pierre-fabre.com); аналитиков узнаём по адресам.
"""

import os
from email.message import EmailMessage as MimeMessage
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from copilot1c import email_intake as ei
from copilot1c import server
from copilot1c.config import Settings

DOMAINS = ("pierre-fabre.com",)
ANALYST_EMAILS = ("i.ivanov@pierre-fabre.com",)
ANALYSTS = ("Иванов И.", "Петрова А.")

# Письмо пользователя: проблема + подпись; ниже — старая переписка (рассылка интегратора и согласование ТЗ),
# её авторы инициаторами быть не должны
USER_LETTER = """Добрый день!

После обновления до 11.5.27.75 не проводится реализация, ошибка:
{Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: Поле объекта не обнаружено (КС_Гамма)

С уважением,
Смирнова Мария
Ведущий бухгалтер, отдел учёта продаж
АО «Пьер Фабр»
Тел.: +7 495 123-45-67
m.smirnova@pierre-fabre.com

From: Petrova Anna <a.petrova@integrator.ru>
Sent: Friday, October 2, 2026 18:00
To: Smirnova Maria <m.smirnova@pierre-fabre.com>
Subject: Обновление УТ 11 завершено

Обновление завершено, база доступна.

From: Petrov Pavel <p.petrov@pierre-fabre.com>
Sent: Monday, March 16, 2026 11:00
To: Petrova Anna <a.petrova@integrator.ru>
Subject: Согласование ТЗ

Согласуем ТЗ ред. 2.
"""


def forwarded_quoted() -> bytes:
    """Аналитик переслал письмо пользователя текстом (цитатой)."""
    m = MimeMessage()
    m["Subject"] = "FW: Не проводится реализация"
    m["From"] = "Иванов Иван <i.ivanov@pierre-fabre.com>"
    m["To"] = "1c-support@pierre-fabre.com"
    m["Date"] = "Tue, 06 Oct 2026 09:00:00 +0300"
    m["Message-ID"] = "<fw-1@pierre-fabre.com>"
    m.set_content("Коллеги, заводим обращение.\n\n"
                  "From: Smirnova Maria <m.smirnova@pierre-fabre.com>\n"
                  "Sent: Monday, October 5, 2026 10:15\n"
                  "To: Ivanov Ivan <i.ivanov@pierre-fabre.com>\n"
                  "Subject: Не проводится реализация\n\n" + USER_LETTER)
    return bytes(m)


def user_original() -> MimeMessage:
    m = MimeMessage()
    m["Subject"] = "Не проводится реализация"
    m["From"] = "Smirnova Maria <m.smirnova@pierre-fabre.com>"
    m["To"] = "Ivanov Ivan <i.ivanov@pierre-fabre.com>"
    m["Date"] = "Mon, 05 Oct 2026 10:15:00 +0300"
    m["Message-ID"] = "<user-42@pierre-fabre.com>"
    m.set_content(USER_LETTER)
    m.add_attachment(b"\x89PNG" + b"0" * 20000, maintype="image", subtype="png", filename="скрин ошибки.png",
                     disposition="inline", cid="<shot1>")
    m.add_attachment(b"\x89PNG" + b"0" * 500, maintype="image", subtype="png", filename="logo.png",
                     disposition="inline", cid="<logo>")  # логотип подписи — не прикрепляется
    m.add_attachment(b"line1\nline2", maintype="text", subtype="plain", filename="журнал.log")
    return m


def forwarded_as_attachment() -> bytes:
    """Аналитик переслал письмо пользователя вложением (message/rfc822)."""
    m = MimeMessage()
    m["Subject"] = "FW: Не проводится реализация"
    m["From"] = "Иванов Иван <i.ivanov@pierre-fabre.com>"
    m["Date"] = "Tue, 06 Oct 2026 09:05:00 +0300"
    m["Message-ID"] = "<fw-2@pierre-fabre.com>"
    m.set_content("См. вложение.")
    m.add_attachment(user_original())
    return bytes(m)


def analyze(data: bytes, name: str = "письмо.eml", **kw):
    kw = {"internal_domains": DOMAINS, "analyst_emails": ANALYST_EMAILS, "analysts": ANALYSTS, **kw}
    return ei.analyze(name, data, **kw)


def test_initiator_from_quoted_forward():
    it = analyze(forwarded_quoted())
    assert it.confidence == "high" and it.initiator.email == "m.smirnova@pierre-fabre.com"
    roles = [(m.email, m.role) for m in it.chain]  # от ранних к поздним
    assert roles == [("p.petrov@pierre-fabre.com", "internal"), ("a.petrova@integrator.ru", "external"),
                     ("m.smirnova@pierre-fabre.com", "internal"), ("i.ivanov@pierre-fabre.com", "analyst")]

    p = ei.proposal("письмо.eml", it)
    d = p["draft"]
    assert d["title"] == "Не проводится реализация" and d["source"] == "email" and d["source_ref"] == "письмо.eml"
    assert d["reported_at"].startswith("2026-10-05T10:15")
    assert "Поле объекта не обнаружено" in d["description"] and "Ведущий бухгалтер" not in d["description"]
    assert d["source_message_id"].startswith("<quote-")  # письмо пользователя здесь — цитата, без Message-ID
    ini = p["initiator"]
    assert ini["position"] == "Ведущий бухгалтер, отдел учёта продаж"
    assert ini["phone"] == "+7 495 123-45-67" and ini["organization"] == "АО «Пьер Фабр»"
    assert [c["chosen"] for c in p["chain"]] == [False, False, True, False]
    assert p["chain"][3]["signature"] is None  # подпись аналитика не разбирается


def test_initiator_from_attached_letter_with_files():
    it = analyze(forwarded_as_attachment())
    assert it.initiator.email == "m.smirnova@pierre-fabre.com" and it.initiator.origin == "nested"
    p = ei.proposal("fw.eml", it)
    assert p["draft"]["source_message_id"] == "<user-42@pierre-fabre.com>"  # Message-ID письма пользователя
    assert [f["filename"] for f in p["files"]] == ["скрин ошибки.png", "журнал.log"]  # логотип отброшен
    # то же письмо, пересланное цитатой другим аналитиком, — другой ключ; файлом — тот же
    assert ei.message_key(analyze(forwarded_as_attachment())) == "<user-42@pierre-fabre.com>"


def test_analysts_only_and_unknown_domains():
    m = MimeMessage()
    m["Subject"] = "Напоминание"
    m["From"] = "Иванов Иван <i.ivanov@pierre-fabre.com>"
    m["Date"] = "Tue, 06 Oct 2026 09:00:00 +0300"
    m.set_content("Проверить выгрузку НСИ.")
    it = analyze(bytes(m))
    assert it.initiator is None and it.confidence == "none" and "аналитики" in it.reason
    # свои домены не заданы: все авторы внешние — выбран последний, с пометкой «проверьте»
    it = analyze(forwarded_quoted(), internal_domains=(), analyst_emails=(), analysts=())
    assert it.confidence == "low" and it.initiator.email == "i.ivanov@pierre-fabre.com" and "проверьте" in it.reason
    # аналитика узнаём и по фамилии из COPILOT_ANALYSTS, если адреса не заданы
    assert ei.classify(ei.ChainMessage("Иванов Иван", "", None, "", "", "quoted", 0), DOMAINS, (), ANALYSTS) == "analyst"


def test_msg_sender_falls_back_to_smtp_for_exchange_x500():
    streams = {"__substg1.0_5D01": "m.smirnova@pierre-fabre.com"}
    msg = SimpleNamespace(sender="Smirnova Maria </O=EXCHANGELABS/OU=EXCHANGE/CN=RECIPIENTS/CN=ABC>",
                          getStringStream=lambda k: streams.get(k))
    assert ei._mapi_sender(msg) == "Smirnova Maria <m.smirnova@pierre-fabre.com>"
    ok = SimpleNamespace(sender="Smirnova Maria <m.smirnova@pierre-fabre.com>", getStringStream=lambda k: None)
    assert ei._mapi_sender(ok) == ok.sender


def test_settings_lists_accept_commas_and_json(monkeypatch):
    monkeypatch.setenv("COPILOT_INTERNAL_DOMAINS", "pierre-fabre.com")
    monkeypatch.setenv("COPILOT_ANALYST_EMAILS", "a@pierre-fabre.com; b@pierre-fabre.com")
    monkeypatch.setenv("COPILOT_ANALYSTS", '["Иванов И.", "Петрова А."]')
    s = Settings(_env_file=None)
    assert s.internal_domains == ("pierre-fabre.com",)
    assert s.analyst_emails == ("a@pierre-fabre.com", "b@pierre-fabre.com") and s.analysts == ANALYSTS


# ---------- API демона ----------

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from copilot1c.graph.store import GraphStore

    s = Settings(pg_dsn=PG_DSN, project="test-proj", issues_dir="issues", cache_dir=str(tmp_path / ".cache"),
                 yc_api_key="", yc_folder_id="", onec_bin="/nonexistent", analysts=ANALYSTS,
                 internal_domains=DOMAINS, analyst_emails=ANALYST_EMAILS)
    g = GraphStore(settings=s)
    g.init_schema()
    g.conn.execute("TRUNCATE issue_events, issue_attachments, issues, contacts RESTART IDENTITY CASCADE")
    g.conn.commit()
    g.close()
    monkeypatch.chdir(tmp_path)
    return TestClient(server.create_app(s))


@needs_pg
def test_from_email_api_and_repeat_protection(client):
    known = client.post("/contacts", json={"name": "Мария Смирнова", "email": "m.smirnova@pierre-fabre.com"}).json()
    files = {"file": ("fw.eml", forwarded_as_attachment(), "message/rfc822")}
    p = client.post("/issues/from-email", files=files).json()
    assert p["initiator"]["email"] == "m.smirnova@pierre-fabre.com" and p["contact"]["id"] == known["id"]
    assert p["already_registered"] is None and p["confidence"] == "high"

    issue = client.post("/issues", json={**p["draft"], "initiator_contact_id": known["id"], "actor": "Иванов И."}).json()
    again = client.post("/issues/from-email", files=files).json()
    assert again["already_registered"] == {"id": issue["id"], "number": issue["number"], "title": issue["title"]}

    assert client.post("/issues/from-email", files={"file": ("a.txt", b"x", "text/plain")}).status_code == 422
    bad = client.post("/issues/from-email", files={"file": ("a.msg", b"not an ole file", "x")})
    assert bad.status_code == 422 and "Не удалось разобрать" in bad.json()["detail"]


@needs_pg
def test_attach_email_with_expand(client):
    iid = client.post("/issues", json={"title": "Ошибка"}).json()["id"]
    r = client.post(f"/issues/{iid}/attachments", files=[("files", ("fw.eml", forwarded_as_attachment(), "x"))],
                    data={"actor": "Иванов И.", "expand": "true"}).json()["attachments"]
    assert [a["filename"] for a in r] == ["fw.eml", "скрин ошибки.png", "журнал.log"]
    assert r[1]["from_email"] == "fw.eml" and r[1]["mime"] == "image/png"
    plain = client.post(f"/issues/{iid}/attachments", files=[("files", ("x.eml", forwarded_quoted(), "x"))]).json()
    assert len(plain["attachments"]) == 1  # без expand — только само письмо
