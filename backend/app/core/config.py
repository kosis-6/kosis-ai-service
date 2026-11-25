from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "kosis-ai-service"
    debug: bool = False

    kosis_api_key: str = ""
    kosis_base_url: str = "https://kosis.kr/openapi"

    hcx_api_key: str = ""
    hcx_base_url: str = "https://clovastudio.stream.ntruss.com"
    hcx_max_concurrency: int = 3
    hcx_retry_budget_seconds: float = 15.0
    question_cache: bool = True
    plan_cache: bool = True
    understand_merged: Literal["off", "gemma"] = "off"
    binding_shadow: bool = False
    binding_use: bool = True
    binding_dict_shadow: bool = False
    binding_dict_gate: bool = False

    search_llm: Literal["auto", "hcx", "gemma"] = "auto"
    gemma_base_url: str = ""
    gemma_api_key: str = ""
    gemma_model: str = "gemma-4-31b"
    gemma_timeout: float = 30.0

    api_docs: bool = False

    database_url: str = "postgresql+psycopg://kosis@localhost:5432/kosis"

    jwt_secret_key: str = ""
    access_token_minutes: int = 15
    refresh_token_days: int = 14
    session_max_days: int = 30
    cookie_secure: bool | None = None

    admin_allow_ips: str = ""

    mail_backend: Literal["smtp", "console"] = "console"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""
    mail_from_name: str = "LIKE SNAKE"

    embedding_backend: Literal["clova", "local", "off"] = "clova"
    llm_pick: bool = True
    llm_pick_mode: Literal["filter", "pick", "pick_decided", "resolve"] | None = None
    llm_pick_together: bool = False
    search_plan: bool = True
    handoff_alternatives: bool = False
    handoff_cell_llm: bool = True

    @field_validator("admin_allow_ips")
    @classmethod
    def _check_admin_ips(cls, value: str) -> str:
        for item in value.split(","):
            if item.strip():
                try:
                    ip_network(item.strip(), strict=False)
                except ValueError as e:
                    raise ValueError(
                        f"ADMIN_ALLOW_IPS의 '{item.strip()}'은 IP나 대역이 아닙니다"
                    ) from e
        return value

    @property
    def admin_networks(self) -> list[IPv4Network | IPv6Network]:
        return [
            ip_network(i.strip(), strict=False)
            for i in self.admin_allow_ips.split(",")
            if i.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
