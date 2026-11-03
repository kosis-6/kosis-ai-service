from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # .env는 레포 최상위(compose가 읽는 위치). backend/에서 실행하면 ../.env
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"), env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "kosis-ai-service"
    debug: bool = False

    kosis_api_key: str = ""
    kosis_base_url: str = "https://kosis.kr/openapi"

    hcx_api_key: str = ""
    hcx_base_url: str = "https://clovastudio.stream.ntruss.com"

    database_url: str = "postgresql+psycopg://kosis@localhost:5432/kosis"

    # 통계표 검색의 질문 임베딩 (app/services/vector_search.py)
    # clova: CLOVA Studio v2 / local: bge-m3 직접 실행 / off: 키워드 검색만
    embedding_backend: Literal["clova", "local", "off"] = "clova"
    # 검색 뒤 HCX-007 최종 검토(후보 5개 중 선택·범위 밖 거르기). LLM_PICK=false 로 끈다
    llm_pick: bool = True
    # filter: 범위 밖 거르기만 (후보 교체 없음, 멤버가 안 정해지면 사용자에게 선택지를 준다).
    # pick: 고른 표로 패밀리를 바꾼다. resolve: 멤버 되묻기를 HCX-007이 대신 확정한다.
    # 실험실 §56-4·레포 측정 §66: 최상 설정 위에서 pick 은 팀원셋 −3(형제 멤버로 바꿈).
    # 11/2 통합 개발셋 224문항: filter 168/206, resolve 170/206 (되묻기 적중 둘 다 12/20).
    # 2문항 차이라 임의 선택보다 사용자 선택을 기본으로 한다
    llm_pick_mode: Literal["filter", "pick", "resolve"] = "filter"

    # pipeline/run_all.py가 만드는 전처리 DB (프로젝트 루트 기준 상대 경로)
    duckdb_path: str = "data/processed/kosis.duckdb"


@lru_cache
def get_settings() -> Settings:
    return Settings()
