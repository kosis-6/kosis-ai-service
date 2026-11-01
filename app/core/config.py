from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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
    # filter: 범위 밖 거르기만 (후보 교체 없음). pick: 고른 표로 패밀리를 바꾼다.
    # 실험실 §56-4·레포 측정 §66: 최상 설정 위에서 pick 은 팀원셋 −3(형제 멤버로 바꿈), filter 는 손해 없음
    llm_pick_mode: Literal["filter", "pick"] = "filter"

    # pipeline/run_all.py가 만드는 전처리 DB (프로젝트 루트 기준 상대 경로)
    duckdb_path: str = "data/processed/kosis.duckdb"


@lru_cache
def get_settings() -> Settings:
    return Settings()
