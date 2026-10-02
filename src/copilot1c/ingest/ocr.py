"""Распознавание текста на изображениях и сканах.

Бэкенды по порядку предпочтения:
  * yandex — Yandex Vision OCR (AI Studio), основной в облаке; данные не покидают Yandex Cloud;
  * tesseract — локальный, для разработки и закрытого контура (нужны языки rus+eng);
  * none — распознавание выключено, изображение помечается как нераспознанное.

У Vision OCR есть квота на частоту запросов (ответ 429). Поэтому запросы идут не чаще ocr_rps в
секунду, при 429/5xx повторяются с паузой (с учётом Retry-After), результаты кэшируются по хэшу
картинки (одни и те же скриншоты приходят во многих письмах, а повторная индексация не должна
снова тратить квоту). Если сервис стабильно отказывает, распознавание отключается до конца запуска,
а индексация продолжается без него.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

from copilot1c.config import Settings, get_settings

OCR_URL = "https://ai.api.cloud.yandex.net/ocr/v1/recognizeText"
MIN_IMAGE_BYTES = 20_000  # меньше — как правило логотипы и иконки подписи
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_CONSECUTIVE_FAILURES = 3  # после стольких отказов подряд OCR выключается до конца запуска


class OcrUnavailable(Exception):
    """Распознать не удалось; текст причины — для отчёта о разборе."""


class _State:
    lock = threading.Lock()
    last_call = 0.0
    failures = 0
    disabled_reason: str | None = None


def reset_state() -> None:
    _State.last_call, _State.failures, _State.disabled_reason = 0.0, 0, None


def _mime(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:4] == b"%PDF":
        return "PDF"
    return None


def backend(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    if s.ocr_backend != "auto":
        return s.ocr_backend
    if s.yc_api_key and s.yc_folder_id:
        return "yandex"
    return "tesseract" if _tesseract_has_rus() else "none"


def _tesseract_has_rus() -> bool:
    """Без русской модели tesseract выдаёт по кириллице мусор — такой бэкенд хуже, чем никакого."""
    if not shutil.which("tesseract"):
        return False
    langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout.split()
    return "rus" in langs


def _throttle(rps: float) -> None:
    if rps <= 0:
        return
    with _State.lock:
        wait = _State.last_call + 1.0 / rps - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _State.last_call = time.monotonic()


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    try:
        return min(float(resp.headers.get("Retry-After", "")), 60.0)
    except ValueError:
        return min(2.0 ** attempt, 30.0)  # 1, 2, 4, 8, 16, 30…


def _yandex(data: bytes, mime: str, s: Settings) -> str:
    post, sleep = httpx.post, time.sleep  # через модуль — чтобы подменять в тестах
    payload = {"mimeType": mime, "languageCodes": ["ru", "en"], "model": "page",
               "content": base64.b64encode(data).decode()}
    headers = {"Authorization": f"Api-Key {s.yc_api_key}", "x-folder-id": s.yc_folder_id}
    for attempt in range(s.ocr_max_retries + 1):
        _throttle(s.ocr_rps)
        try:
            resp = post(OCR_URL, headers=headers, json=payload, timeout=120)
        except httpx.TransportError as exc:
            if attempt == s.ocr_max_retries:
                raise OcrUnavailable(f"Vision OCR недоступен: {type(exc).__name__}") from exc
            sleep(min(2.0 ** attempt, 30.0))
            continue
        if resp.status_code in RETRY_STATUSES and attempt < s.ocr_max_retries:
            sleep(_retry_after(resp, attempt))
            continue
        if resp.status_code == 429:
            raise OcrUnavailable("Vision OCR: превышена квота запросов (429) — увеличьте квоту или уменьшите "
                                 "COPILOT_OCR_RPS")
        if resp.status_code in (401, 403):
            raise OcrUnavailable(f"Vision OCR: нет доступа ({resp.status_code}) — нужна роль ai.vision.user "
                                 "у сервисного аккаунта")
        if resp.status_code >= 400:
            raise OcrUnavailable(f"Vision OCR: ошибка {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        body = body.get("result", body)
        return (body.get("textAnnotation") or {}).get("fullText", "")
    raise OcrUnavailable("Vision OCR: исчерпаны повторы")


def _tesseract(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
        f.write(data)
        name = f.name
    try:
        out = subprocess.run(["tesseract", name, "-", "-l", "rus+eng"], capture_output=True, text=True, timeout=300)
        return out.stdout
    finally:
        Path(name).unlink(missing_ok=True)


def _cache_path(data: bytes, s: Settings) -> Path | None:
    if not s.cache_dir:
        return None
    return Path(s.cache_dir) / "ocr" / f"{hashlib.sha256(data).hexdigest()}.txt"


def ocr(data: bytes, settings: Settings | None = None) -> str | None:
    """Текст изображения или одностраничного PDF.

    None — распознавание выключено или формат не поддержан; OcrUnavailable — сервис отказал
    (вызывающий код фиксирует причину в отчёте и продолжает индексацию).
    """
    s = settings or get_settings()
    mime = _mime(data)
    b = backend(s)
    if mime is None or b == "none" or (b == "tesseract" and mime == "PDF"):
        return None
    cache = _cache_path(data, s)
    if cache is not None and cache.exists():
        return cache.read_text(encoding="utf-8")
    if _State.disabled_reason:
        raise OcrUnavailable(_State.disabled_reason)

    try:
        text = (_yandex(data, mime, s) if b == "yandex" else _tesseract(data)).strip()
    except OcrUnavailable as exc:
        _State.failures += 1
        if _State.failures >= MAX_CONSECUTIVE_FAILURES:
            _State.disabled_reason = f"{exc} (распознавание отключено до конца запуска)"
        raise
    _State.failures = 0
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")
    return text
