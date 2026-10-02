import httpx
import pytest

from copilot1c.config import Settings
from copilot1c.ingest import ocr as ocr_mod
from copilot1c.ingest.attachments import Skipped, parse_bytes
from copilot1c.ingest.ocr import OcrUnavailable, ocr

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 30_000


@pytest.fixture
def settings(tmp_path):
    ocr_mod.reset_state()
    return Settings(yc_api_key="k", yc_folder_id="f", ocr_backend="yandex", ocr_rps=0, ocr_max_retries=3,
                    cache_dir=str(tmp_path / "cache"))


def _responses(monkeypatch, statuses):
    calls, sleeps = [], []

    def post(url, headers, json, timeout):
        calls.append(url)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        body = {"result": {"textAnnotation": {"fullText": "Связанные документы"}}} if status == 200 else {}
        return httpx.Response(status, json=body, headers={"Retry-After": "2"} if status == 429 else {})

    monkeypatch.setattr(ocr_mod.httpx, "post", post)
    monkeypatch.setattr(ocr_mod.time, "sleep", sleeps.append)
    return calls, sleeps


def test_retry_on_429_then_success_and_cache(monkeypatch, settings):
    calls, sleeps = _responses(monkeypatch, [429, 429, 200])
    assert ocr(PNG, settings) == "Связанные документы"
    assert len(calls) == 3 and sleeps == [2.0, 2.0]  # пауза из Retry-After
    assert ocr(PNG, settings) == "Связанные документы"
    assert len(calls) == 3  # второй раз — из кэша, квота не тратится


def test_persistent_429_disables_ocr_but_indexing_continues(monkeypatch, settings):
    calls, _ = _responses(monkeypatch, [429])
    with pytest.raises(OcrUnavailable, match="квота"):
        ocr(PNG, settings)
    assert len(calls) == 4  # 1 + 3 повтора

    results = [parse_bytes(PNG + bytes([i]), f"img{i}.png", f"s{i}", settings=settings) for i in range(3)]
    assert all(isinstance(r[0], Skipped) for r in results)
    assert "отключено до конца запуска" in results[-1][0].reason
    assert len(calls) == 4 + 4 + 4  # после трёх отказов подряд сервис больше не дёргается


def test_auth_error_is_not_retried(monkeypatch, settings):
    calls, _ = _responses(monkeypatch, [403])
    with pytest.raises(OcrUnavailable, match="ai.vision.user"):
        ocr(PNG, settings)
    assert len(calls) == 1
