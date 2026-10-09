"""Небо для секретаря: события Солнца, Луны, планет и ярчайших звёзд — для сообщений в момент события.

Считается локально библиотекой ephem (теории движения встроены, сеть не нужна; точность — доли минуты):
  Солнце и Луна — восход, верхняя кульминация (с высотой), заход; Луна — ещё фазы (новолуние, первая четверть,
  полнолуние, последняя четверть) и освещённость в момент восхода;
  планеты (Меркурий … Нептун) и 30 ярчайших звёзд — верхняя кульминация и высота над горизонтом в этот момент.
Восход и заход — по стандарту астрономических ежегодников: верхний край диска, рефракция на горизонте 34′
(давление 0 и горизонт −0°34′). Кульминации ниже горизонта (звезда в этом месте не восходит) не выдаются.
Полярный день или ночь — восхода и захода нет, это не ошибка.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo

import ephem

PLANETS = [("mercury", "Меркурий", ephem.Mercury), ("venus", "Венера", ephem.Venus), ("mars", "Марс", ephem.Mars),
           ("jupiter", "Юпитер", ephem.Jupiter), ("saturn", "Сатурн", ephem.Saturn), ("uranus", "Уран", ephem.Uranus),
           ("neptune", "Нептун", ephem.Neptune)]

# 30 ярчайших звёзд неба (по видимой звёздной величине), имена — как в каталоге ephem, и по-русски
STARS = [("Sirius", "Сириус"), ("Canopus", "Канопус"), ("Rigil Kentaurus", "Альфа Центавра"), ("Arcturus", "Арктур"),
         ("Vega", "Вега"), ("Capella", "Капелла"), ("Rigel", "Ригель"), ("Procyon", "Процион"),
         ("Achernar", "Ахернар"), ("Betelgeuse", "Бетельгейзе"), ("Hadar", "Хадар"), ("Altair", "Альтаир"),
         ("Acrux", "Акрукс"), ("Aldebaran", "Альдебаран"), ("Antares", "Антарес"), ("Spica", "Спика"),
         ("Pollux", "Поллукс"), ("Fomalhaut", "Фомальгаут"), ("Deneb", "Денеб"), ("Mimosa", "Мимоза"),
         ("Regulus", "Регул"), ("Adhara", "Адара"), ("Castor", "Кастор"), ("Shaula", "Шаула"), ("Gacrux", "Гакрукс"),
         ("Bellatrix", "Беллатрикс"), ("Elnath", "Эльнат"), ("Miaplacidus", "Миаплацидус"), ("Alnilam", "Альнилам"),
         ("Alnair", "Альнаир")]

PHASES = [("new_moon", "Новолуние", ephem.next_new_moon), ("first_quarter", "Первая четверть Луны",
                                                             ephem.next_first_quarter_moon),
          ("full_moon", "Полнолуние", ephem.next_full_moon), ("last_quarter", "Последняя четверть Луны",
                                                               ephem.next_last_quarter_moon)]


@dataclass
class SkyEvent:
    body: str            # sun | moon | mars | Sirius …
    kind: str            # rise | transit | set | new_moon | first_quarter | full_moon | last_quarter
    at: datetime         # UTC
    text: str            # готовое сообщение (время — местное)
    alt: float | None = None
    data: dict = field(default_factory=dict)


def _dt(d: ephem.Date) -> datetime:
    return ephem.Date(d).datetime().replace(tzinfo=UTC)


def _deg(x: float) -> str:
    return f"{math.degrees(x):.1f}".replace(".", ",")


def _observer(lat: float, lon: float) -> ephem.Observer:
    o = ephem.Observer()
    o.lat, o.lon = str(lat), str(lon)
    o.pressure = 0            # рефракцию на горизонте задаёт horizon (стандарт ежегодников)
    o.horizon = "-0:34"
    return o


def _at(o: ephem.Observer, when: datetime) -> ephem.Observer:
    c = o.copy()
    c.date = ephem.Date(when.astimezone(UTC).replace(tzinfo=None))
    return c


def _alt(o: ephem.Observer, body, when: datetime) -> float:
    c = _at(o, when)
    body.compute(c)
    return float(body.alt)


def moon_state(when: datetime) -> tuple[float, bool]:
    """(освещённость %, растущая ли) в момент when."""
    utc = when.astimezone(UTC).replace(tzinfo=None)
    m1, m2 = ephem.Moon(ephem.Date(utc)), ephem.Moon(ephem.Date(utc + timedelta(hours=1)))
    return float(m1.phase), float(m2.phase) > float(m1.phase)


def _times(o: ephem.Observer, body, fn: str, start: datetime, end: datetime) -> list[datetime]:
    """Все моменты next_rising / next_transit / next_setting в [start, end]. Полярные случаи — пусто."""
    out, cur = [], start
    for _ in range(4):
        c = _at(o, cur)
        try:
            t = _dt(getattr(c, fn)(body))
        except (ephem.AlwaysUpError, ephem.NeverUpError):
            return out
        if t > end:
            break
        out.append(t)
        cur = t + timedelta(minutes=10)
    return out


def events(lat: float, lon: float, tz: tzinfo, start: datetime, end: datetime, planets: bool = True,
           stars: bool = True) -> list[SkyEvent]:
    """События неба в месте (lat, lon) с start по end (UTC), по времени. Время в текстах — в поясе tz."""
    o = _observer(lat, lon)
    hm = lambda t: t.astimezone(tz).strftime("%H:%M")  # noqa: E731
    out: list[SkyEvent] = []

    def daylight(t: datetime) -> bool:
        return math.degrees(_alt(o, ephem.Sun(), t)) > -6  # светлее гражданских сумерек — звёзд не видно

    # Солнце
    for t in _times(o, ephem.Sun(), "next_rising", start, end):
        sets = _times(o, ephem.Sun(), "next_setting", t, t + timedelta(hours=24))
        length = ""
        if sets:
            m = int((sets[0] - t).total_seconds() // 60)
            length = f" Долгота дня {m // 60} ч {m % 60:02d} мин."
        out.append(SkyEvent("sun", "rise", t, f"Восход Солнца — {hm(t)}.{length}"))
    for t in _times(o, ephem.Sun(), "next_transit", start, end):
        alt = _alt(o, ephem.Sun(), t)
        if alt > 0:
            out.append(SkyEvent("sun", "transit", t, f"Верхняя кульминация Солнца — {hm(t)}, высота {_deg(alt)}°.",
                                math.degrees(alt)))
    for t in _times(o, ephem.Sun(), "next_setting", start, end):
        out.append(SkyEvent("sun", "set", t, f"Заход Солнца — {hm(t)}."))

    # Луна
    for t in _times(o, ephem.Moon(), "next_rising", start, end):
        pct, waxing = moon_state(t)
        out.append(SkyEvent("moon", "rise", t, f"Восход Луны — {hm(t)}. Луна {'растущая' if waxing else 'убывающая'}, "
                                              f"освещена на {pct:.0f} %.", data={"illumination": round(pct, 1),
                                                                                 "waxing": waxing}))
    for t in _times(o, ephem.Moon(), "next_transit", start, end):
        alt = _alt(o, ephem.Moon(), t)
        if alt > 0:
            out.append(SkyEvent("moon", "transit", t, f"Верхняя кульминация Луны — {hm(t)}, высота {_deg(alt)}°.",
                                math.degrees(alt)))
    for t in _times(o, ephem.Moon(), "next_setting", start, end):
        out.append(SkyEvent("moon", "set", t, f"Заход Луны — {hm(t)}."))
    for kind, name, fn in PHASES:
        t = _dt(fn(ephem.Date(start.replace(tzinfo=None))))
        if t <= end:
            out.append(SkyEvent("moon", kind, t, f"{name} — {hm(t)} ({t.astimezone(tz).strftime('%d.%m')})."))

    # Планеты и звёзды — верхняя кульминация над горизонтом
    targets = ([(key, name, cls()) for key, name, cls in PLANETS] if planets else []) + \
              ([(key, name, ephem.star(key)) for key, name in STARS] if stars else [])
    for key, name, body in targets:
        for t in _times(o, body, "next_transit", start, end):
            alt = _alt(o, body, t)
            if alt <= 0:
                continue  # в этом месте не восходит
            day = daylight(t)
            note = " (светло — не видно)" if day else ""
            out.append(SkyEvent(key, "transit", t, f"Верхняя кульминация: {name} — {hm(t)}, высота {_deg(alt)}°{note}.",
                                math.degrees(alt), {"name": name, "daylight": day,
                                                    "kind": "planet" if key in {p[0] for p in PLANETS} else "star"}))
    return sorted(out, key=lambda e: e.at)
