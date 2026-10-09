"""Погода сейчас в городе — только средствами Yandex Cloud.

Yandex Search API v2 (тот же ключ AI Studio, что у поиска агента в интернете, copilot1c/web.py), два шага:
1. Страница выдачи целиком (FORMAT_HTML) по запросу «погода <город>»: на ней обычно есть блок Яндекса с погодой
   сейчас («Сейчас +8°, ощущается как +5°, облачно…»). Из текста страницы берутся куски вокруг «°».
2. Если там погоды нет — сниппеты обычной выдачи (FORMAT_XML) по сайту yandex.ru.
Модель COPILOT_MODEL_BATCH (structured output) вынимает текущую погоду только из этого текста; прогноз на день
и на завтра не берётся. Не нашлось факта — погода не выдумывается, в ответе «не удалось получить».
Проверка на хосте: copilot1c weather Брянск — что дал каждый шаг.

Результат кэшируется на 10 минут на город: несколько сессий подряд не тратят запросы Search API.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any
from urllib.parse import quote

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


def current_weather(city: str, country: str, settings, search=None, extract=None, page=None,
                    steps: list | None = None) -> dict[str, Any]:
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
        result = _fetch(city, country, settings, search, extract, page, steps)
    except Exception as exc:  # noqa: BLE001 — погода не должна ломать сообщение об окончании сессии
        return {"ok": False, "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if result.get("ok"):
        with _LOCK:
            _CACHE[key] = (now, result)
    return result


_DEGREE = re.compile(r"[+\-−–]?\s?\d{1,2}\s?°")


def weather_window(text: str, chars: int = 600, limit: int = 4) -> str:
    """Куски страницы вокруг первых значений температуры («+8°») — модели не нужна вся страница выдачи."""
    out, last_end = [], -1
    for m in _DEGREE.finditer(text):
        a, b = max(0, m.start() - chars // 2), min(len(text), m.end() + chars // 2)
        if a <= last_end:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
        last_end = b
        if len(out) >= limit:
            break
    return "\n…\n".join(text[a:b] for a, b in out)


def _extract(city: str, country: str, text: str, settings, extract) -> dict[str, Any]:
    if extract is not None:
        return extract(city, country, text)
    from copilot1c.index.yandex import chat_json

    return chat_json(
        f"Город: {city} ({country}).\n\nТекст:\n{text}", WEATHER_SCHEMA, model=settings.model_batch, settings=settings,
        system="Извлеки текущую погоду (то, что «сейчас») в указанном городе только из текста. Прогноз на день, на "
               "завтра и на неделю не подходит. Ничего не придумывай: нет в тексте — не заполняй поле; нет текущей "
               "погоды — found=false. source — номер фрагмента [N], если фрагменты пронумерованы.")


def _result(data: dict[str, Any], url: str, title: str) -> dict[str, Any]:
    w = {k: data[k] for k in ("temperature_c", "feels_like_c", "condition", "wind_ms", "humidity", "observed")
         if data.get(k) not in (None, "")}
    return {"ok": True, **w, "text": describe(w), "url": url, "title": title}


def _fetch(city: str, country: str, settings, search, extract, page=None, steps: list | None = None) -> dict[str, Any]:
    from copilot1c.web import WebError, web_search, web_search_page

    steps = steps if steps is not None else []
    # 1. страница выдачи с блоком погоды
    page = page or web_search_page
    try:
        got = page(f"погода {city}", settings)
        window = weather_window(got.get("text", ""))
        steps.append({"step": "страница выдачи", "query": got.get("query_sent"), "chars": len(got.get("text", "")),
                      "excerpt": window[:1500]})
        if window:
            data = _extract(city, country, window, settings, extract)
            steps[-1]["model"] = data
            if data.get("found") and data.get("temperature_c") is not None:
                return _result(data, "https://yandex.ru/search/?text=" + quote(f"погода {city}"), "Яндекс")
    except WebError as exc:
        steps.append({"step": "страница выдачи", "error": str(exc)})
    # 2. сниппеты выдачи
    search = search or web_search
    found = search(f"погода {city} сейчас", settings, sites=["yandex.ru"], k=5)
    results = found.get("results", [])
    # Сначала страницы Яндекс Погоды, затем прочие страницы yandex.ru
    results.sort(key=lambda r: 0 if "pogoda" in r.get("url", "") or "weather" in r.get("url", "") else 1)
    results = [r for r in results if r.get("snippet")][:5]
    steps.append({"step": "сниппеты", "query": found.get("query_sent"),
                  "results": [{"url": r["url"], "snippet": r["snippet"][:300]} for r in results]})
    if not results:
        return {"ok": False, "reason": "поиск Яндекса не вернул страниц погоды"}
    text = "\n\n".join(f"[{i}] {r['title']} — {r['url']}\n{r['snippet']}" for i, r in enumerate(results, 1))
    data = _extract(city, country, text, settings, extract)
    steps[-1]["model"] = data
    if not data.get("found") or data.get("temperature_c") is None:
        top = results[0]
        return {"ok": False, "reason": "в выдаче нет текущей погоды", "url": top["url"], "title": top["title"]}
    n = data.get("source") or 1
    src = results[n - 1] if 1 <= n <= len(results) else results[0]
    return _result(data, src["url"], src["title"])


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()
