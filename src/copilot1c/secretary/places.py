"""Город из фразы → название в именительном падеже, страна, часовой пояс (IANA).

Только свои средства и Yandex AI Studio: частые города — встроенный справочник (без запросов наружу),
остальное — модель COPILOT_MODEL_BATCH со structured output. Часовой пояс от модели проверяется по базе
часовых поясов хоста (zoneinfo): неизвестный пояс не принимается. Найденное моделью кэшируется в таблице
sec_place_names, чтобы один и тот же город не спрашивать дважды.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

# Город → (страна, часовой пояс). Падежные формы строятся автоматически (_forms), особые — в ALIASES.
KNOWN: dict[str, tuple[str, str]] = {
    "Варшава": ("Польша", "Europe/Warsaw"), "Краков": ("Польша", "Europe/Warsaw"),
    "Гданьск": ("Польша", "Europe/Warsaw"), "Вроцлав": ("Польша", "Europe/Warsaw"),
    "Познань": ("Польша", "Europe/Warsaw"), "Лодзь": ("Польша", "Europe/Warsaw"),
    "Лондон": ("Великобритания", "Europe/London"), "Манчестер": ("Великобритания", "Europe/London"),
    "Эдинбург": ("Великобритания", "Europe/London"), "Дублин": ("Ирландия", "Europe/Dublin"),
    "Москва": ("Россия", "Europe/Moscow"), "Санкт-Петербург": ("Россия", "Europe/Moscow"),
    "Казань": ("Россия", "Europe/Moscow"), "Нижний Новгород": ("Россия", "Europe/Moscow"),
    "Екатеринбург": ("Россия", "Asia/Yekaterinburg"), "Новосибирск": ("Россия", "Asia/Novosibirsk"),
    "Калининград": ("Россия", "Europe/Kaliningrad"), "Сочи": ("Россия", "Europe/Moscow"),
    "Брянск": ("Россия", "Europe/Moscow"), "Воронеж": ("Россия", "Europe/Moscow"), "Тула": ("Россия", "Europe/Moscow"),
    "Калуга": ("Россия", "Europe/Moscow"), "Смоленск": ("Россия", "Europe/Moscow"), "Курск": ("Россия", "Europe/Moscow"),
    "Белгород": ("Россия", "Europe/Moscow"), "Липецк": ("Россия", "Europe/Moscow"), "Тверь": ("Россия", "Europe/Moscow"),
    "Ярославль": ("Россия", "Europe/Moscow"), "Владимир": ("Россия", "Europe/Moscow"),
    "Рязань": ("Россия", "Europe/Moscow"), "Иваново": ("Россия", "Europe/Moscow"), "Кострома": ("Россия", "Europe/Moscow"),
    "Тамбов": ("Россия", "Europe/Moscow"), "Пенза": ("Россия", "Europe/Moscow"), "Псков": ("Россия", "Europe/Moscow"),
    "Великий Новгород": ("Россия", "Europe/Moscow"), "Мурманск": ("Россия", "Europe/Moscow"),
    "Архангельск": ("Россия", "Europe/Moscow"), "Вологда": ("Россия", "Europe/Moscow"),
    "Петрозаводск": ("Россия", "Europe/Moscow"), "Ростов-на-Дону": ("Россия", "Europe/Moscow"),
    "Краснодар": ("Россия", "Europe/Moscow"), "Ставрополь": ("Россия", "Europe/Moscow"),
    "Махачкала": ("Россия", "Europe/Moscow"), "Чебоксары": ("Россия", "Europe/Moscow"),
    "Киров": ("Россия", "Europe/Kirov"), "Волгоград": ("Россия", "Europe/Volgograd"),
    "Саратов": ("Россия", "Europe/Saratov"), "Самара": ("Россия", "Europe/Samara"),
    "Ульяновск": ("Россия", "Europe/Ulyanovsk"), "Астрахань": ("Россия", "Europe/Astrakhan"),
    "Ижевск": ("Россия", "Europe/Samara"), "Уфа": ("Россия", "Asia/Yekaterinburg"),
    "Пермь": ("Россия", "Asia/Yekaterinburg"), "Челябинск": ("Россия", "Asia/Yekaterinburg"),
    "Тюмень": ("Россия", "Asia/Yekaterinburg"), "Оренбург": ("Россия", "Asia/Yekaterinburg"),
    "Омск": ("Россия", "Asia/Omsk"), "Томск": ("Россия", "Asia/Tomsk"), "Барнаул": ("Россия", "Asia/Barnaul"),
    "Кемерово": ("Россия", "Asia/Novokuznetsk"), "Красноярск": ("Россия", "Asia/Krasnoyarsk"),
    "Иркутск": ("Россия", "Asia/Irkutsk"), "Якутск": ("Россия", "Asia/Yakutsk"),
    "Хабаровск": ("Россия", "Asia/Vladivostok"), "Владивосток": ("Россия", "Asia/Vladivostok"),
    "Магадан": ("Россия", "Asia/Magadan"), "Петропавловск-Камчатский": ("Россия", "Asia/Kamchatka"),
    "Каир": ("Египет", "Africa/Cairo"),
    "Хельсинки": ("Финляндия", "Europe/Helsinki"), "Таллин": ("Эстония", "Europe/Tallinn"),
    "Рига": ("Латвия", "Europe/Riga"), "Вильнюс": ("Литва", "Europe/Vilnius"), "Минск": ("Беларусь", "Europe/Minsk"),
    "Стокгольм": ("Швеция", "Europe/Stockholm"), "Осло": ("Норвегия", "Europe/Oslo"),
    "Копенгаген": ("Дания", "Europe/Copenhagen"), "Берлин": ("Германия", "Europe/Berlin"),
    "Мюнхен": ("Германия", "Europe/Berlin"), "Франкфурт": ("Германия", "Europe/Berlin"),
    "Гамбург": ("Германия", "Europe/Berlin"), "Вена": ("Австрия", "Europe/Vienna"), "Прага": ("Чехия", "Europe/Prague"),
    "Будапешт": ("Венгрия", "Europe/Budapest"), "Братислава": ("Словакия", "Europe/Bratislava"),
    "Белград": ("Сербия", "Europe/Belgrade"), "Бухарест": ("Румыния", "Europe/Bucharest"),
    "София": ("Болгария", "Europe/Sofia"), "Афины": ("Греция", "Europe/Athens"),
    "Париж": ("Франция", "Europe/Paris"), "Тулуза": ("Франция", "Europe/Paris"), "Лион": ("Франция", "Europe/Paris"),
    "Ницца": ("Франция", "Europe/Paris"), "Кастр": ("Франция", "Europe/Paris"),
    "Брюссель": ("Бельгия", "Europe/Brussels"), "Амстердам": ("Нидерланды", "Europe/Amsterdam"),
    "Люксембург": ("Люксембург", "Europe/Luxembourg"), "Цюрих": ("Швейцария", "Europe/Zurich"),
    "Женева": ("Швейцария", "Europe/Zurich"), "Рим": ("Италия", "Europe/Rome"), "Милан": ("Италия", "Europe/Rome"),
    "Мадрид": ("Испания", "Europe/Madrid"), "Барселона": ("Испания", "Europe/Madrid"),
    "Лиссабон": ("Португалия", "Europe/Lisbon"), "Стамбул": ("Турция", "Europe/Istanbul"),
    "Анталья": ("Турция", "Europe/Istanbul"), "Тбилиси": ("Грузия", "Asia/Tbilisi"),
    "Ереван": ("Армения", "Asia/Yerevan"), "Баку": ("Азербайджан", "Asia/Baku"),
    "Алматы": ("Казахстан", "Asia/Almaty"), "Астана": ("Казахстан", "Asia/Almaty"),
    "Ташкент": ("Узбекистан", "Asia/Tashkent"), "Дубай": ("ОАЭ", "Asia/Dubai"),
    "Нью-Йорк": ("США", "America/New_York"), "Бостон": ("США", "America/New_York"),
}
ALIASES = {"питер": "Санкт-Петербург", "питере": "Санкт-Петербург", "петербург": "Санкт-Петербург",
           "петербурге": "Санкт-Петербург", "спб": "Санкт-Петербург", "мск": "Москва", "нижний": "Нижний Новгород",
           "нижнем новгороде": "Нижний Новгород", "нижний новгород": "Нижний Новгород", "софии": "София",
           "софию": "София", "афинах": "Афины", "афины": "Афины", "warsaw": "Варшава", "warszawa": "Варшава",
           "london": "Лондон", "moscow": "Москва", "helsinki": "Хельсинки", "paris": "Париж",
           "berlin": "Берлин", "toulouse": "Тулуза", "нью-йорке": "Нью-Йорк", "анталье": "Анталья",
           "анталью": "Анталья", "алмате": "Алматы", "алма-ате": "Алматы", "кастре": "Кастр",
           "ростове-на-дону": "Ростов-на-Дону", "ростов": "Ростов-на-Дону", "ростове": "Ростов-на-Дону",
           "великом новгороде": "Великий Новгород", "новгороде": "Великий Новгород", "иванове": "Иваново",
           "петропавловске-камчатском": "Петропавловск-Камчатский", "екб": "Екатеринбург"}
# Не места: «я в офисе», «я в отпуске», «я в пути»
NOT_PLACES = {"офис", "офисе", "отпуск", "отпуске", "пути", "дороге", "самолёте", "самолете", "поезде", "машине",
              "такси", "порядке", "работе", "деле", "курсе", "теме", "шоке", "командировке", "отеле", "гостинице",
              "аэропорту", "зале", "переговорке", "совещании", "зуме", "zoom", "теамс", "teams", "очереди", "кафе",
              "больнице", "больничном", "декрете", "строю", "ударе", "настроении", "норме", "сети"}


@dataclass
class Place:
    city: str
    country: str
    tz: str
    source: str = "справочник"   # справочник | модель | кэш


class NotAPlace(Exception):
    """Слово не город («офис», «отпуск») или город не распознан — причина текстом для ответа."""


def _forms(name: str) -> set[str]:
    """Падежные формы названия: Варшава → варшаве, варшаву, варшавы; Лондон → лондоне, лондона; Казань → казани."""
    low = name.lower()
    out = {low}
    if " " in low:
        return out
    if low.endswith("а"):
        stem = low[:-1]
        out |= {stem + e for e in ("е", "у", "ы", "ой", "и")}
    elif low.endswith("я"):
        stem = low[:-1]
        out |= {stem + e for e in ("и", "ю", "е", "ей")}
    elif low.endswith("ь"):
        stem = low[:-1]
        out |= {stem + e for e in ("и", "е", "ью", "я", "ем")}
    elif low[-1] not in "аеёиоуыэюя":
        out |= {low + e for e in ("е", "а", "у", "ом")}
    return out


_INDEX: dict[str, str] = {}
for _city in KNOWN:
    for _f in _forms(_city):
        _INDEX.setdefault(_f, _city)
for _alias, _city in ALIASES.items():
    _INDEX[_alias] = _city


def normalize_key(said: str) -> str:
    return " ".join(re.sub(r"[^\wё\s-]", " ", said.lower().replace("ё", "е")).split())


def known(said: str) -> Place | None:
    key = normalize_key(said)
    city = _INDEX.get(key) or _INDEX.get(key.replace("е", "ё"))
    if city is None:
        return None
    country, tz = KNOWN[city]
    return Place(city, country, tz)


def valid_tz(tz: str) -> bool:
    if not tz or tz not in available_timezones():
        return False
    try:
        ZoneInfo(tz)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


# ---------- часовой пояс из ответа модели ----------
# Модель не всегда называет пояс идентификатором IANA: для российских городов бывает «MSK», «МСК+2», «UTC+3»,
# «Москва». Поэтому пояс ищется по шагам: точный IANA → тот же без учёта регистра → сокращения МСК → смещение UTC
# плюс страна по базе часовых поясов хоста (zone.tab: страна → её пояса) → смещение без страны (Etc/GMT±N).

ZONE_TAB = ("/usr/share/zoneinfo/zone.tab", "/usr/share/zoneinfo/zone1970.tab")
_MSK = re.compile(r"^(?:msk|мск)\s*(?:([+\-−])\s*(\d{1,2}))?$", re.IGNORECASE)
_OFFSET = re.compile(r"(?:utc|gmt|мск|msk)?\s*([+\-−])\s*(\d{1,2})(?::?(\d{2}))?", re.IGNORECASE)
_ZONES_BY_COUNTRY: dict[str, list[str]] | None = None


def _country_zones() -> dict[str, list[str]]:
    """ISO-код страны → пояса в порядке zone.tab (первым идёт главный: RU → Kaliningrad, Moscow, …)."""
    global _ZONES_BY_COUNTRY
    if _ZONES_BY_COUNTRY is None:
        out: dict[str, list[str]] = {}
        for path in ZONE_TAB:
            try:
                lines = open(path, encoding="utf-8").read().splitlines()  # noqa: SIM115
            except OSError:
                continue
            for line in lines:
                if line.startswith("#") or not line.strip():
                    continue
                cols = line.split("\t")
                if len(cols) >= 3:
                    for code in cols[0].split(","):
                        zones = out.setdefault(code.upper(), [])
                        if cols[2] not in zones:
                            zones.append(cols[2])
        _ZONES_BY_COUNTRY = out
    return _ZONES_BY_COUNTRY


def _offset_minutes(text: str) -> int | None:
    m = _OFFSET.search(text or "")
    if not m:
        return None
    sign = -1 if m.group(1) in "-−" else 1
    return sign * (int(m.group(2)) * 60 + int(m.group(3) or 0))


def _current_offset(tz: str) -> int:
    from datetime import datetime

    off = datetime.now(ZoneInfo(tz)).utcoffset()
    return int(off.total_seconds() // 60) if off is not None else 0


def pick_tz(raw: str, country_code: str = "", utc_offset: str = "") -> str | None:
    """Пояс IANA по тому, что вернула модель; None — определить нельзя."""
    raw = (raw or "").strip()
    if valid_tz(raw):
        return raw
    names = {z.lower(): z for z in available_timezones()}
    if raw.lower().replace(" ", "_") in names:
        return names[raw.lower().replace(" ", "_")]
    code = (country_code or "").strip().upper()[:2]
    offset = None
    m = _MSK.match(raw)
    if m:  # МСК, МСК+2 — российское время
        code = code or "RU"
        offset = 180 + (int(m.group(2)) * (-1 if m.group(1) in "-−" else 1) if m.group(2) else 0) * 60
    if offset is None:
        offset = _offset_minutes(utc_offset)
    if offset is None:
        offset = _offset_minutes(raw)
    zones = _country_zones().get(code, []) if code else []
    zones = [z for z in zones if valid_tz(z)]
    if zones and offset is not None:
        same = [z for z in zones if _current_offset(z) == offset]
        if same:
            return same[0]
    if len(zones) == 1:
        return zones[0]
    if offset is not None and offset % 60 == 0 and -12 * 60 <= offset <= 14 * 60:
        h = offset // 60
        return "Etc/UTC" if h == 0 else f"Etc/GMT{'-' if h > 0 else '+'}{abs(h)}"  # у Etc/GMT знак обратный
    return None


PLACE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_place": {"type": "boolean", "description": "true, если это населённый пункт (город, посёлок)"},
        "city": {"type": "string", "description": "название по-русски в именительном падеже"},
        "country": {"type": "string", "description": "страна по-русски"},
        "country_code": {"type": "string", "description": "код страны ISO 3166-1 alpha-2, например RU, PL"},
        "timezone": {"type": "string", "description": "часовой пояс IANA, например Europe/Moscow, Europe/Warsaw"},
        "utc_offset": {"type": "string", "description": "смещение от UTC сейчас, например +03:00"},
    },
    "required": ["is_place", "city", "country", "country_code", "timezone", "utc_offset"],
}


def ask_model(said: str, settings) -> Place:
    """Город, который не знает справочник, — моделью AI Studio. NotAPlace, если модель не уверена или пояс неверный."""
    from copilot1c.index.yandex import chat_json

    if not (settings.yc_api_key and settings.yc_folder_id):
        raise NotAPlace(f"не знаю город «{said}», а модель недоступна (нет ключа AI Studio)")
    data = chat_json(
        f"Пользователь сказал, что находится или едет: «в {said}». Определи населённый пункт.",
        PLACE_SCHEMA, model=settings.model_batch, settings=settings,
        system="Ты определяешь город по слову в любом падеже. Если это не населённый пункт (офис, отпуск, дорога) "
               "или ты не уверен, верни is_place=false. Часовой пояс — идентификатор IANA (Europe/Moscow, "
               "Asia/Yekaterinburg), не «MSK» и не «UTC+3»; отдельно — смещение от UTC и код страны.")
    if not data.get("is_place") or not data.get("city"):
        raise NotAPlace(f"«{said}» — не похоже на город")
    raw = str(data.get("timezone", "")).strip()
    tz = pick_tz(raw, str(data.get("country_code", "")), str(data.get("utc_offset", "")))
    if tz is None:
        import logging

        logging.getLogger("copilot1c.secretary").warning("пояс не определён: %s", data)
        raise NotAPlace(f"для «{data['city']}» не удалось определить часовой пояс (модель ответила «{raw or '—'}»)")
    return Place(str(data["city"]).strip(), str(data.get("country", "")).strip(), tz, "модель")


def resolve(said: str, settings, cache=None) -> Place:
    """Справочник → кэш (sec_place_names) → модель. cache — объект с get(key)/put(key, place) или None."""
    key = normalize_key(said)
    if not key:
        raise NotAPlace("город не указан")
    if key in NOT_PLACES or key.split()[0] in NOT_PLACES:
        raise NotAPlace(f"«{said}» — не город")
    p = known(said)
    if p:
        return p
    if cache is not None:
        hit = cache.get(key)
        if hit is not None:
            return hit
    p = ask_model(said, settings)
    alias = known(p.city)  # модель могла назвать город из справочника — берём проверенный пояс
    if alias:
        p = Place(alias.city, alias.country, alias.tz, "модель")
    if cache is not None:
        cache.put(key, p)
    return p
