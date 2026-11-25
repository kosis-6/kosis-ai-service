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
    plan_cache: bool = False
    understand_merged: Literal["off", "gemma"] = "off"
    # 답이 나간 뒤 Gemma 1회가 그 답이 질문에 맞는지 판정하고, 틀렸다고 보면 한 번 다시 계산한다
    # (app/services/answer_judge.py, ask_session._judged). 사전 지표로 답했으면 통계표 찾기로,
    # 통계표에서 계산했으면 그 표를 빼고 다음 후보로. 다시 한 답이 판정을 통과할 때만 바꾼다.
    # 함께 켜지는 것: 조건이 모두 맞고 2위와 점수 차가 큰 1위 표는 검토(llm_pick)를 건너뛴다.
    # 기본은 끔 (ANSWER_JUDGE=true 로 켠다). GEMMA_BASE_URL 이 없으면 켜도 판정하지 않는다
    answer_judge: bool = False
    # 이어 묻는 말 판단을 LLM이 먼저 (실험실 A_16, 11/25): 직전 질문 · 조건 · 답을 주고
    # followup / new / unclear 와 혼자 서는 완전한 질문 한 문장을 받아 새 질문 경로에 넣는다.
    # 켜지면 ratio_followup 부터 뒤의 코드 규칙(비율 후속 · 지역만 바꾸기 · 구별로 · 구성비로 ·
    # 사람 수 묻기)과 변경 추출은 쓰지 않는다. 그보다 앞의 규칙(되묻기 답 · 인사 · 완전한 질문
    # complete_question · 요약 · 출처 · 왜)은 그대로 먼저 돈다. 복원 호출이 실패하면 기존 경로로.
    # 기본은 끔 (FOLLOW_RESTATE=true 로 켠다). 꺼지면 지금 구조(규칙 + FOLLOW_SYSTEM 변경 추출)
    follow_restate: bool = False
    # 제3안 3-a (FOLLOW_RESTATE=true 와 함께 켤 때만): 대화 셋에서 오판이 없던 코드 규칙
    # (비율 후속 · 지역만 바꾸기 · 구별로)이 잡는 말은 LLM 복원 없이 그 규칙으로 처리하고,
    # 나머지만 LLM 복원으로 보낸다. 직전 답이 정상(ok)일 때만 규칙을 쓴다 (거절 뒤 "부산은?"이
    # 거절된 조건을 이어받던 C27). 기본은 끔 (FOLLOW_SURE_RULES=true 로 켠다)
    follow_sure_rules: bool = False
    # 제3안 3-b: 기존 경로(코드 규칙 + 변경 추출)로 이어 묻기를 처리한 뒤, LLM 이 그 해석이
    # 새 말과 맞는지 한 번 더 본다. 어긋난다고 하면 LLM 이 쓴 완전한 질문으로 새 질문 경로에서
    # 다시 계산한다. FOLLOW_RESTATE 가 복원으로 처리한 턴에는 걸리지 않는다.
    # 기본은 끔 (FOLLOW_CHECK=true 로 켠다)
    follow_check: bool = False
    # 조건 → 칸 잇기 1단계 (app/services/binding.py, docs/cell_binding_design_2026-11-23.md).
    # true: 바로 계산이 표 하나를 계산할 때 같은 표로 잇기를 돌려 처리 기록에만 남긴다
    # (답은 그대로).
    # 개발셋 평가에서 BINDING_SHADOW=true로 켜고 scripts/binding_compare.py로 견준다
    binding_shadow: bool = False
    binding_use: bool = True
    binding_dict_shadow: bool = False
    binding_dict_gate: bool = True

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
