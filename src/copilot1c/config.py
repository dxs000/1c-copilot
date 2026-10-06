"""Настройки из переменных окружения (префикс COPILOT_) и файла .env."""

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Список в .env можно задать JSON-ом (["a", "b"]) или через запятую (a, b) — второе привычнее и не роняет
# демон, если кавычки забыли
StrList = Annotated[tuple[str, ...], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COPILOT_", env_file=".env", extra="ignore")

    # Yandex AI Studio (OpenAI-совместимый API)
    yc_folder_id: str = ""
    yc_api_key: str = ""
    ai_base_url: str = "https://ai.api.cloud.yandex.net/v1"

    # Имена моделей без префикса gpt://<folder>/ — префикс добавляет model_uri()
    model_orchestrator: str = "gpt-oss-120b/latest"
    model_code: str = "qwen3-235b-a22b-fp8/latest"
    model_long_context: str = "deepseek-v4-flash/latest"
    model_business_text: str = "aliceai-llm"
    model_batch: str = "yandexgpt-lite/latest"
    embedding_doc: str = "text-search-doc/latest"
    embedding_query: str = "text-search-query/latest"

    # Хранилища
    vector_store_id: str = ""  # индекс AI Studio Vector Store (создаётся командой create-index)
    pg_dsn: str = "postgresql://copilot:copilot@localhost:5432/copilot"
    s3_bucket: str = "copilot1c-sources"
    project: str = Field(default="ut11-update", description="Код проекта для фильтров индекса")

    # Распознавание изображений и сканов: auto | yandex | tesseract | none
    ocr_backend: str = "auto"
    ocr_rps: float = 1.0  # не чаще N запросов в секунду к Vision OCR (квота каталога)
    ocr_max_retries: int = 5  # повторы при 429/5xx с паузой
    cache_dir: str = ".cache"  # кэш результатов OCR по хэшу картинки; пусто — без кэша
    # Подпапки материалов, которые не индексируются при обходе папки: эталонные наборы eval, выгрузки кода
    # 1С (индексируются отдельно) и uploads — загрузки через веб обрабатывает демон в порядке загрузки.
    # Указанная явно папка (index-docs data/eval) всё равно читается.
    ingest_exclude_dirs: tuple[str, ...] = ("eval", "dumps", "uploads")
    # Материалы проекта (корпус) и куда демон сохраняет загруженные через веб
    corpus_dir: str = "data"
    materials_dir: str = "data/uploads"
    # Обращения: вложения хранятся в <issues_dir>/<id>/ — вне data, чтобы не попасть в индекс.
    # Аналитики интегратора — для полей «кто завёл» и «ответственный» (в .env JSON-списком:
    # COPILOT_ANALYSTS='["Иванов И.", "Петрова А."]'); пустой список — имя вводится вручную.
    issues_dir: str = "issues"
    analysts: StrList = ()
    # Разбор письма в обращение: свои домены (аналитики и инициаторы — сотрудники одной организации) и
    # адреса аналитиков. Инициатор — последний автор цепочки из своих доменов, который не аналитик.
    # Тип сообщения в чате: неуверенные случаи эвристик уточняет модель (model_batch); false — только эвристики
    intent_llm: bool = True
    # Поиск в интернете для агента (Yandex Search API v2, ключ AI Studio): выключатель, лимиты на один вопрос,
    # запрещённые для отправки слова сверх своих доменов и аналитиков (название заказчика и т. п.)
    web_search: bool = True
    web_max_searches: int = 3
    web_max_pages: int = 3
    web_blocked_terms: StrList = ("Pierre Fabre", "Пьер Фабр")
    internal_domains: StrList = ()
    analyst_emails: StrList = ()
    # Конвертация старых форматов (.doc, .xls, .rtf, .odt) через LibreOffice
    soffice_bin: str = "soffice"

    # Платформа 1С на ВМ-песочнице
    onec_bin: str = "/opt/1cv8/x86_64/8.3.27.2342/1cv8"
    ibcmd_bin: str = "/opt/1cv8/x86_64/8.3.27.2342/ibcmd"
    sandbox_ib_path: str = "/var/lib/copilot1c/sandbox-ib"

    # Префиксы доработок интегратора: по ним отделяются нетиповые объекты
    custom_prefixes: tuple[str, ...] = ("КС_", "(КС)")

    @field_validator("analysts", "internal_domains", "analyst_emails", "web_blocked_terms", mode="before")
    @classmethod
    def _str_list(cls, v):
        if isinstance(v, str):
            v = v.strip()
            items = json.loads(v) if v.startswith("[") else v.replace(";", ",").split(",")
            return tuple(str(x).strip() for x in items if str(x).strip())
        return v

    def model_uri(self, name: str) -> str:
        return f"gpt://{self.yc_folder_id}/{name}"

    def embedding_uri(self, name: str) -> str:
        return f"emb://{self.yc_folder_id}/{name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
