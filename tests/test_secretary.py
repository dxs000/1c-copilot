"""Секретарь: разбор фраз, города, сессии с таймером, сообщение об окончании (местное время и погода).

Разбор и города — без базы. Сценарии — на живом PostgreSQL из COPILOT_TEST_PG_DSN (таблицы sec_* очищаются);
время — подставные часы, погода и модель — заглушки (наружу ничего не уходит).
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta

import pytest

from copilot1c.config import Settings
from copilot1c.secretary import weather as weather_mod
from copilot1c.secretary.parse import parse, parse_date, parse_duration, parse_until
from copilot1c.secretary.places import NotAPlace, known, resolve

PG_DSN = os.environ.get("COPILOT_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not PG_DSN, reason="нет COPILOT_TEST_PG_DSN — тесты с PostgreSQL пропущены")
FRI = date(2026, 10, 9)  # пятница


# ---------- длительности, даты ----------

@pytest.mark.parametrize("text,minutes", [
    ("работаем час", 60), ("работаем один час", 60), ("работаем 1 час", 60), ("полчаса", 30),
    ("полтора часа", 90), ("2 часа", 120), ("45 минут", 45), ("сорок пять минут", 45), ("1 ч 20 мин", 80),
    ("1:30", 90), ("90м", 90), ("перерыв 15 мин", 15), ("четверть часа", 15), ("работаем 25", 25),
    ("работаем 2", 120), ("двадцать минут", 20), ("работаем", None), ("работаем 13 часов", None),
])
def test_duration(text, minutes):
    assert parse_duration(text) == minutes


def test_until_and_dates():
    assert parse_until("работаю до 18:00") == time(18, 0) and parse_until("до 9.30") == time(9, 30)
    assert parse_until("работаем час") is None
    assert parse_date("завтра поехал", FRI) == date(2026, 10, 10)
    assert parse_date("послезавтра", FRI) == date(2026, 10, 11)
    assert parse_date("в понедельник лечу", FRI) == date(2026, 10, 12)
    assert parse_date("в пятницу лечу", FRI) == date(2026, 10, 16)  # сегодня пятница — следующая
    assert parse_date("12 октября", FRI) == date(2026, 10, 12) and parse_date("15.11", FRI) == date(2026, 11, 15)
    assert parse_date("5 января", FRI) == date(2027, 1, 5)  # уже прошло в этом году — следующий
    assert parse_date("через 3 дня", FRI) == date(2026, 10, 12) and parse_date("я в Варшаве", FRI) is None


# ---------- фразы ----------

def _one(text):
    cmds = parse(text, FRI)
    assert len(cmds) == 1, cmds
    return cmds[0]


def test_place_phrases():
    c = _one("Я в Варшаве")
    assert (c.action, c.place, c.when, c.direction) == ("place", "Варшаве", None, "arrive")
    c = _one("Завтра поехал в Лондон")
    assert (c.action, c.place, c.when, c.direction) == ("place", "Лондон", date(2026, 10, 10), "depart")
    c = _one("вернулся в Варшаву")
    assert (c.place, c.when) == ("Варшаву", None)
    assert _one("я в понедельник в Лондоне").place == "Лондоне"  # «в понедельник» — дата, не место
    assert _one("в Хельсинки").place == "Хельсинки"
    assert _one("лечу в Нижний Новгород в субботу").place == "Нижний Новгород"
    assert _one("еду в Санкт-Петербург по работе").place == "Санкт-Петербург"


def test_session_and_other_phrases():
    c = _one("работаем один час")
    assert (c.action, c.minutes) == ("work", 60)
    c = _one("перерыв 15 минут")
    assert (c.action, c.minutes) == ("rest", 15)
    c = _one("работаю до 18:30")
    assert (c.action, c.until, c.minutes) == ("work", time(18, 30), None)
    assert _one("работаем").minutes is None  # без срока — спросим
    assert _one("стоп").action == "stop" and _one("закончили работу").action == "stop"
    assert _one("сколько осталось?").action == "status" and _one("сколько я уже работаю").action == "status"
    assert _one("где я").action == "where" and _one("какая погода").action == "where"
    assert _one("где я был").action == "history" and _one("не еду").action == "cancel_trip"
    assert [c.action for c in parse("я в Варшаве, работаем час", FRI)] == ["place", "work"]
    assert parse("купи молока", FRI) == []


# ---------- города ----------

def test_known_cities_and_not_places():
    s = Settings(yc_api_key="", yc_folder_id="")
    for said, city, tz in [("Варшаве", "Варшава", "Europe/Warsaw"), ("Варшаву", "Варшава", "Europe/Warsaw"),
                           ("Лондон", "Лондон", "Europe/London"), ("Лондоне", "Лондон", "Europe/London"),
                           ("Москве", "Москва", "Europe/Moscow"), ("Питере", "Санкт-Петербург", "Europe/Moscow"),
                           ("Хельсинки", "Хельсинки", "Europe/Helsinki"), ("Казани", "Казань", "Europe/Moscow"),
                           ("Нижний Новгород", "Нижний Новгород", "Europe/Moscow")]:
        p = resolve(said, s)
        assert (p.city, p.tz) == (city, tz), said
    with pytest.raises(NotAPlace):
        resolve("офисе", s)
    with pytest.raises(NotAPlace, match="модель недоступна"):
        resolve("Урюпинске", s)  # нет в справочнике, нет ключа — не выдумываем
    assert known("Тулузе").country == "Франция"


def test_unknown_city_goes_to_model_and_bad_tz_rejected(monkeypatch):
    from copilot1c.index import yandex

    s = Settings(yc_api_key="k", yc_folder_id="f")
    answers = iter([{"is_place": True, "city": "Пятигорск", "country": "Россия", "timezone": "Europe/Moscow"},
                    {"is_place": True, "city": "Атлантида", "country": "?", "timezone": "Atlantis/Main"}])
    monkeypatch.setattr(yandex, "chat_json", lambda *a, **kw: next(answers))
    cache: dict = {}

    class Cache:
        get = staticmethod(cache.get)

        @staticmethod
        def put(k, v):
            cache[k] = v

    assert resolve("Пятигорске", s, Cache).tz == "Europe/Moscow" and "пятигорске" in cache
    assert resolve("Пятигорске", s, Cache).city == "Пятигорск"  # второй раз — из кэша, модель не зовём
    with pytest.raises(NotAPlace, match="часовой пояс"):
        resolve("Атлантиде", s, Cache)


# ---------- погода ----------

def test_weather_from_yandex_search(monkeypatch):
    weather_mod.clear_cache()
    s = Settings(yc_api_key="k", yc_folder_id="f")
    calls = []

    def search(q, settings, sites=None, k=5):
        calls.append((q, sites))
        return {"results": [
            {"title": "Погода в Варшаве на 10 дней", "url": "https://yandex.ru/pogoda/warsaw/details", "snippet": "…"},
            {"title": "Погода в Варшаве сейчас", "url": "https://yandex.ru/pogoda/warsaw",
             "snippet": "Сейчас +12°, ощущается как +9°. Облачно. Ветер 4 м/с. Влажность 81%"}]}

    def extract(city, country, text):
        assert "[2]" in text and "ощущается как +9" in text
        return {"found": True, "temperature_c": 12, "feels_like_c": 9, "condition": "Облачно", "wind_ms": 4,
                "humidity": 81, "source": 2}

    w = weather_mod.current_weather("Варшава", "Польша", s, search=search, extract=extract)
    assert w["ok"] and w["text"] == "+12 °C, ощущается как +9, облачно, ветер 4 м/с, влажность 81 %"
    assert w["url"] == "https://yandex.ru/pogoda/warsaw" and calls == [("погода Варшава Польша сейчас", ["yandex.ru"])]
    assert weather_mod.current_weather("Варшава", "Польша", s, search=search, extract=extract)["cached"]
    assert len(calls) == 1  # второй раз — из кэша

    weather_mod.clear_cache()
    no_fact = weather_mod.current_weather("Варшава", "Польша", s, search=search,
                                          extract=lambda *a: {"found": False})
    assert not no_fact["ok"] and no_fact["url"].startswith("https://yandex.ru/pogoda")
    assert not weather_mod.current_weather("Варшава", "Польша", Settings(yc_api_key="", yc_folder_id=""))["ok"]


# ---------- сценарии с базой ----------

class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def tick(self, **kw) -> None:
        self.at += timedelta(**kw)


WEATHER = {"ok": True, "text": "+12 °C, облачно", "url": "https://yandex.ru/pogoda/warsaw"}


@pytest.fixture
def sec():
    from copilot1c.graph.store import GraphStore
    from copilot1c.secretary.service import Secretary

    s = Settings(pg_dsn=PG_DSN, yc_api_key="", yc_folder_id="", secretary_llm=False)
    g = GraphStore(settings=s)
    clock = Clock(datetime(2026, 10, 9, 5, 0, tzinfo=UTC))  # 07:00 в Варшаве (UTC+2)
    weather_calls = []
    sx = Secretary(g.conn, s, now=clock, weather=lambda c, k: weather_calls.append(c) or WEATHER)
    for t in ("sec_notices", "sec_sessions", "sec_places", "sec_place_names"):
        g.conn.execute(f"DELETE FROM {t}")
    g.conn.commit()
    sx.clock, sx.weather_calls = clock, weather_calls
    yield sx
    g.close()


@needs_pg
def test_places_journey(sec):
    r = sec.say("ivan", "Я в Варшаве")
    assert "Записал: вы в городе Варшава (Польша)" in r["reply"] and "07:00" in r["reply"]
    assert r["state"]["place"]["city"] == "Варшава" and r["state"]["utc_offset"] == "UTC+02:00"
    assert "Уже записано" in sec.say("ivan", "я в Варшаве")["reply"]

    r = sec.say("ivan", "Завтра поехал в Лондон")
    assert "с 10 октября (суббота) — Лондон" in r["reply"] and "Сейчас вы в городе Варшава" in r["reply"]
    assert r["state"]["place"]["city"] == "Варшава" and r["state"]["upcoming"][0]["city"] == "Лондон"
    assert r["state"]["upcoming"][0]["date"] == "2026-10-10"  # полночь по Варшаве — в Лондоне ещё 9-е
    assert "Запланировано: с 10 октября (суббота) — Лондон" in sec.say("ivan", "я в Варшаве")["reply"]

    sec.clock.tick(days=1)  # суббота 07:00 UTC+2 → уже Лондон, 06:00 UTC+1
    st = sec.state("ivan")
    assert st["place"]["city"] == "Лондон" and st["utc_offset"] == "UTC+01:00" and st["upcoming"] == []
    assert st["local_time"].startswith("2026-10-10T06:00")

    sec.clock.tick(days=2)
    r = sec.say("ivan", "Вернулся в Варшаву")
    assert "Записал: вы в городе Варшава" in r["reply"] and "До этого: Лондон" in r["reply"]
    assert sec.state("ivan")["place"]["city"] == "Варшава"
    assert sec.state("someone-else")["place"] is None  # у каждого своё
    hist = sec.say("ivan", "где я был")["reply"]
    assert hist.index("Варшава") < hist.index("Лондон") and "• 10.10.2026 — Лондон" in hist


@needs_pg
def test_trip_cancel_and_early_arrival(sec):
    sec.say("ivan", "я в Варшаве")
    sec.say("ivan", "в понедельник лечу в Лондон")
    assert "Отменил: Лондон" in sec.say("ivan", "не еду")["reply"] and sec.state("ivan")["upcoming"] == []
    sec.say("ivan", "завтра еду в Лондон")
    sec.say("ivan", "я в Лондоне")  # приехал раньше — план снимается
    st = sec.state("ivan")
    assert st["place"]["city"] == "Лондон" and st["upcoming"] == []


@needs_pg
def test_work_session_timer_message(sec):
    sec.say("ivan", "я в Варшаве")
    r = sec.say("ivan", "работаем один час")
    assert "Работаем 1 ч — до 08:00 (Варшава, UTC+02:00)" in r["reply"]
    assert r["state"]["session"]["remaining_s"] == 3600

    sec.clock.tick(minutes=59, seconds=59)
    assert sec.finish_due() == [] and sec.state("ivan")["session"]["remaining_s"] == 1
    sec.clock.tick(seconds=3)  # таймер сработал через 2 с после окончания
    done = sec.finish_due()
    assert len(done) == 1 and sec.weather_calls == ["Варшава"]
    text = done[0]["text"]
    assert text.startswith("Рабочая сессия 1 ч закончилась.")
    assert "Варшава: 08:00:00, пятница, 9 октября (UTC+02:00)." in text  # точное местное время окончания
    assert "Погода сейчас: +12 °C, облачно (Яндекс Погода)." in text and "опоздани" not in text
    assert done[0]["data"]["tz"] == "Europe/Warsaw" and done[0]["data"]["weather"]["ok"]

    st = sec.state("ivan")
    assert st["session"] is None and [n["id"] for n in st["unread"]] == [done[0]["id"]]
    assert sec.store.mark_read("ivan", done[0]["id"]) and sec.state("ivan")["unread"] == []
    assert "работа 1 ч" in sec.say("ivan", "сколько осталось")["reply"]


@needs_pg
def test_rest_stop_replace_and_late(sec):
    r = sec.say("ivan", "перерыв 15 минут")  # место не указано — время сервера и подсказка
    assert "Перерыв 15 мин" in r["reply"] and "Где вы?" in r["reply"]
    sec.clock.tick(minutes=5)
    r = sec.say("ivan", "работаем полчаса")
    assert "остановил через 5 мин" in r["reply"]
    sec.clock.tick(minutes=10)
    assert "осталось 20 мин" in sec.say("ivan", "сколько осталось")["reply"]
    assert "прошло 10 мин из 30 мин" in sec.say("ivan", "стоп")["reply"]
    assert sec.say("ivan", "стоп")["reply"] == "Сейчас таймер не идёт."

    sec.say("ivan", "я в Лондоне")
    sec.say("ivan", "работаю до 7:00")  # 06:15 по Лондону → 45 мин
    assert sec.store.running("ivan")["minutes"] == 45
    sec.clock.tick(minutes=50)  # ядро «лежало» 5 минут
    text = sec.finish_due()[0]["text"]
    assert "Лондон: 07:00:00" in text and "с опозданием на 5 мин" in text
    assert "уже прошло" in sec.say("ivan", "работаю до 6:00")["reply"]


@needs_pg
def test_stopped_while_finishing_gives_no_message(sec):
    sec.say("ivan", "работаем 25 минут")
    sec.clock.tick(minutes=26)
    claimed = sec.store.claim_due(sec.now())
    assert len(claimed) == 1
    sec.store._rows("UPDATE sec_sessions SET status = 'stopped' WHERE id = %s", (claimed[0]["id"],))
    assert sec.store.finish(claimed[0], "x", {}, sec.now()) == {}
    assert sec.store.recover() == 0


@needs_pg
def test_api(monkeypatch):
    from fastapi.testclient import TestClient

    from copilot1c import server
    from copilot1c.graph.store import GraphStore

    s = Settings(pg_dsn=PG_DSN, yc_api_key="", yc_folder_id="", secretary_llm=False, secretary_weather=False)
    g = GraphStore(settings=s)
    from copilot1c.secretary.store import ensure_schema

    ensure_schema(g.conn)
    for t in ("sec_notices", "sec_sessions", "sec_places"):
        g.conn.execute(f"DELETE FROM {t}")
    g.conn.commit()
    g.close()
    c = TestClient(server.create_app(s))
    r = c.post("/secretary/say", json={"person": "Иван", "text": "Я в Варшаве"})
    assert r.status_code == 200 and r.json()["state"]["place"]["city"] == "Варшава"
    r = c.post("/secretary/say", json={"person": "Иван", "text": "работаем час"})
    assert r.json()["actions"][0]["session"]["minutes"] == 60
    assert c.get("/secretary/state", params={"person": "Иван"}).json()["session"]["kind"] == "work"
    assert c.get("/secretary/places", params={"person": "Иван"}).json()["places"][0]["city"] == "Варшава"
    assert c.get("/secretary/notices", params={"person": "Иван"}).json() == {"notices": []}
    assert c.post("/secretary/notices/999999/read", json={"person": "Иван"}).status_code == 404
    assert c.post("/secretary/say", json={"person": "Иван", "text": ""}).status_code == 422
