"""Настройки из переменных окружения (префикс COPILOT_) и файла .env."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    pg_dsn: str = "postgresql://copilot:copilot@localhost:5432/copilot"
    s3_bucket: str = "copilot1c-sources"
    project: str = Field(default="ut11-update", description="Код проекта для фильтров индекса")

    # Платформа 1С на ВМ-песочнице
    onec_bin: str = "/opt/1cv8/x86_64/8.3.27.2342/1cv8"
    ibcmd_bin: str = "/opt/1cv8/x86_64/8.3.27.2342/ibcmd"
    sandbox_ib_path: str = "/var/lib/copilot1c/sandbox-ib"

    # Префиксы доработок интегратора: по ним отделяются нетиповые объекты
    custom_prefixes: tuple[str, ...] = ("КС_", "(КС)")

    def model_uri(self, name: str) -> str:
        return f"gpt://{self.yc_folder_id}/{name}"

    def embedding_uri(self, name: str) -> str:
        return f"emb://{self.yc_folder_id}/{name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
