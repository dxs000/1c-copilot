"""Распознавание текста на изображениях и сканах.

Бэкенды по порядку предпочтения:
  * yandex — Yandex Vision OCR (AI Studio), основной в облаке; данные не покидают Yandex Cloud;
  * tesseract — локальный, для разработки и закрытого контура (нужны языки rus+eng);
  * none — распознавание выключено, изображение помечается как нераспознанное.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from copilot1c.config import Settings, get_settings

OCR_URL = "https://ai.api.cloud.yandex.net/ocr/v1/recognizeText"
MIN_IMAGE_BYTES = 20_000  # меньше — как правило логотипы и иконки подписи


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


def _yandex(data: bytes, mime: str, s: Settings) -> str:
    resp = httpx.post(
        OCR_URL,
        headers={"Authorization": f"Api-Key {s.yc_api_key}", "x-folder-id": s.yc_folder_id},
        json={"mimeType": mime, "languageCodes": ["ru", "en"], "model": "page",
              "content": base64.b64encode(data).decode()},
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    body = body.get("result", body)
    return (body.get("textAnnotation") or {}).get("fullText", "")


def _tesseract(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as f:
        f.write(data)
        name = f.name
    try:
        langs = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
        lang = "+".join(lang_ for lang_ in ("rus", "eng") if f"\n{lang_}" in langs) or "eng"
        out = subprocess.run(["tesseract", name, "-", "-l", lang], capture_output=True, text=True, timeout=300)
        return out.stdout
    finally:
        Path(name).unlink(missing_ok=True)


def ocr(data: bytes, settings: Settings | None = None) -> str | None:
    """Текст изображения или одностраничного PDF; None — распознавание недоступно или формат не поддержан."""
    s = settings or get_settings()
    mime = _mime(data)
    if mime is None:
        return None
    b = backend(s)
    if b == "yandex":
        return _yandex(data, mime, s).strip()
    if b == "tesseract" and mime != "PDF":
        return _tesseract(data).strip()
    return None
