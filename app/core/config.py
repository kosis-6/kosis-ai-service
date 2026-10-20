from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "kosis-ai-service"
    debug: bool = False

    kosis_api_key: str = ""
    kosis_base_url: str = "https://kosis.kr/openapi"


@lru_cache
def get_settings() -> Settings:
    return Settings()
