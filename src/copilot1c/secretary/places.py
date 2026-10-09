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
           "анталью": "Анталья", "алмате": "Алматы", "алма-ате": "Алматы", "кастре": "Кастр"}
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


PLACE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_place": {"type": "boolean", "description": "true, если это населённый пункт (город, посёлок)"},
        "city": {"type": "string", "description": "название по-русски в именительном падеже"},
        "country": {"type": "string", "description": "страна по-русски"},
        "timezone": {"type": "string", "description": "часовой пояс IANA, например Europe/Warsaw"},
    },
    "required": ["is_place", "city", "country", "timezone"],
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
               "или ты не уверен, верни is_place=false. Часовой пояс — точный идентификатор IANA.")
    if not data.get("is_place") or not data.get("city"):
        raise NotAPlace(f"«{said}» — не похоже на город")
    tz = str(data.get("timezone", "")).strip()
    if not valid_tz(tz):
        raise NotAPlace(f"для «{data['city']}» не удалось определить часовой пояс")
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
