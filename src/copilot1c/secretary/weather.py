"""Погода сейчас в городе — только средствами Yandex Cloud.

1. Yandex Search API v2 (тот же ключ AI Studio, что у поиска агента в интернете, copilot1c/web.py): запрос
   «погода <город> сейчас» по сайту yandex.ru — в выдаче страницы Яндекс Погоды с фактом («сейчас +12°,
   ощущается как +10°, облачно…»).
2. Модель COPILOT_MODEL_BATCH (structured output) вынимает из найденных фрагментов текущую погоду — только то,
   что написано в тексте; прогноз на день и на завтра не берётся. Не нашлось факта — погода не выдумывается,
   в ответе «не удалось получить» и ссылка.

Результат кэшируется на 10 минут на город: несколько сессий подряд не тратят запросы Search API.
"""

from __future__ import annotations

import threading
import time
from typing import Any

CACHE_SECONDS = 600
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.Lock()

WEATHER_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean", "description": "в тексте есть погода СЕЙЧАС (факт), а не только прогноз"},
        "temperature_c": {"type": "number"},
        "feels_like_c": {"type": "number"},
        "condition": {"type": "string", "description": "облачно, ясно, дождь… — как в тексте, по-русски"},
        "wind_ms": {"type": "number"},
        "humidity": {"type": "integer"},
        "observed": {"type": "string", "description": "время наблюдения, если указано в тексте"},
        "source": {"type": "integer", "description": "номер фрагмента, из которого взята погода"},
    },
    "required": ["found"],
}


def _sign(v: float) -> str:
    v = round(v)
    return f"+{v}" if v > 0 else str(v).replace("-", "−")


def describe(w: dict[str, Any]) -> str:
    """«+12 °C, ощущается как +10, облачно, ветер 4 м/с, влажность 81 %»."""
    parts = []
    if w.get("temperature_c") is not None:
        parts.append(f"{_sign(w['temperature_c'])} °C")
    if w.get("feels_like_c") is not None and round(w["feels_like_c"]) != round(w.get("temperature_c", 1e9)):
        parts.append(f"ощущается как {_sign(w['feels_like_c'])}")
    if w.get("condition"):
        parts.append(str(w["condition"]).strip().rstrip(".").lower())
    if w.get("wind_ms") is not None:
        parts.append(f"ветер {round(w['wind_ms'])} м/с")
    if w.get("humidity") is not None:
        parts.append(f"влажность {w['humidity']} %")
    return ", ".join(parts)


def current_weather(city: str, country: str, settings, search=None, extract=None) -> dict[str, Any]:
    """{ok, text, temperature_c…, url, title} или {ok: False, reason}. search/extract — для тестов."""
    if not getattr(settings, "secretary_weather", True):
        return {"ok": False, "reason": "погода выключена (COPILOT_SECRETARY_WEATHER=false)"}
    if not (settings.yc_api_key and settings.yc_folder_id):
        return {"ok": False, "reason": "нет ключа AI Studio"}
    key = f"{city}|{country}".lower()
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < CACHE_SECONDS:
            return {**hit[1], "cached": True}
    try:
        result = _fetch(city, country, settings, search, extract)
    except Exception as exc:  # noqa: BLE001 — погода не должна ломать сообщение об окончании сессии
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if result.get("ok"):
        with _LOCK:
            _CACHE[key] = (now, result)
    return result


def _fetch(city: str, country: str, settings, search, extract) -> dict[str, Any]:
    from copilot1c.web import web_search

    search = search or web_search
    found = search(f"погода {city} {country} сейчас".replace("  ", " "), settings, sites=["yandex.ru"], k=5)
    results = found.get("results", [])
    # Сначала страницы Яндекс Погоды, затем прочие страницы yandex.ru
    results.sort(key=lambda r: 0 if "pogoda" in r.get("url", "") or "weather" in r.get("url", "") else 1)
    results = [r for r in results if r.get("snippet")][:5]
    if not results:
        return {"ok": False, "reason": "поиск Яндекса не вернул страниц погоды"}
    text = "\n\n".join(f"[{i}] {r['title']} — {r['url']}\n{r['snippet']}" for i, r in enumerate(results, 1))
    if extract is None:
        from copilot1c.index.yandex import chat_json

        data = chat_json(
            f"Город: {city} ({country}).\n\nФрагменты страниц:\n{text}", WEATHER_SCHEMA,
            model=settings.model_batch, settings=settings,
            system="Извлеки текущую погоду (то, что «сейчас») в указанном городе только из фрагментов. Прогноз на "
                   "день, на завтра и на неделю не подходит. Ничего не придумывай: нет в тексте — не заполняй "
                   "поле; нет текущей погоды — found=false.")
    else:
        data = extract(city, country, text)
    if not data.get("found") or data.get("temperature_c") is None:
        top = results[0]
        return {"ok": False, "reason": "в выдаче нет текущей погоды", "url": top["url"], "title": top["title"]}
    n = data.get("source") or 1
    src = results[n - 1] if 1 <= n <= len(results) else results[0]
    w = {k: data[k] for k in ("temperature_c", "feels_like_c", "condition", "wind_ms", "humidity", "observed")
         if data.get(k) not in (None, "")}
    return {"ok": True, **w, "text": describe(w), "url": src["url"], "title": src["title"]}


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
