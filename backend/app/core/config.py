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

    # 통계표 찾기(조건 추출·계획·최종 검토)에 쓰는 LLM (app/services/search_llm.py)
    # auto(기본): GEMMA_BASE_URL이 있으면 Gemma 4 31B, 없으면 HCX-007
    # hcx / gemma: 주소와 관계없이 그 모델로 고정 (gemma인데 주소가 없으면 호출이 실패한다)
    # Gemma는 OpenAI 호환 서버(vLLM)의 /chat/completions로 부른다.
    # 수치 답변(qa)과 연속 대화(ask_session)는 이 설정과 관계없이 HCX-007을 쓴다 (Gemma로 재지 않음)
    search_llm: Literal["auto", "hcx", "gemma"] = "auto"
    gemma_base_url: str = ""  # 예: http://gemma-host:8000/v1. 넣으면 통계표 찾기가 Gemma로 간다
    gemma_api_key: str = ""  # 서버가 키를 받지 않으면 비워 둔다
    gemma_model: str = "gemma-4-31b"  # 서버가 띄운 모델 이름
    gemma_timeout: float = 120.0  # 초. 응답 시간 중앙값이 HCX의 약 2배(9초)다

    # /docs · /redoc · /openapi.json 을 열어 둘지. 기본은 닫힘: 열어 두면 관리자 API 목록까지
    # 누구나 본다. 설정을 빠뜨린 배포 서버가 열린 채로 뜨지 않게 켜는 쪽을 직접 적도록 했다.
    # 개발에서는 API_DOCS=true (compose.yaml 에 들어 있다. 화면 타입 생성 npm run gen:api 에 쓴다)
    api_docs: bool = False

    database_url: str = "postgresql+psycopg://kosis@localhost:5432/kosis"

    # 로그인 토큰(JWT) 서명 키. 32자 이상, 비어 있으면 로그인 API가 503을 돌려준다
    # 만들기: python -c "import secrets; print(secrets.token_urlsafe(48))"
    jwt_secret_key: str = ""
    # Access Token은 짧게, Refresh Token(HttpOnly 쿠키, 서버는 app.user_sessions에 해시만)은 길게
    access_token_minutes: int = 15
    refresh_token_days: int = 14  # 마지막으로 새 토큰을 받은 뒤 이 기간 안 쓰면 다시 로그인
    # 계속 쓰더라도 로그인한 지 이 기간이 지나면 다시 로그인 (훔친 Refresh Token을 끝없이 못 쓰게)
    session_max_days: int = 30
    # Refresh Token 쿠키를 HTTPS에서만 보낼지(Secure). 비워 두면 요청이 https일 때 자동으로 붙인다.
    # 프록시(nginx, ALB) 뒤면 uvicorn이 X-Forwarded-Proto를 믿어야 https로 보인다:
    # FORWARDED_ALLOW_IPS=프록시 주소 (backend/Dockerfile). true/false로 강제할 수도 있다
    cookie_secure: bool | None = None

    # 회원가입 이메일 인증번호 메일 (app/services/mailer.py)
    # smtp: 실제로 보낸다 (Gmail은 앱 비밀번호, 배포는 AWS SES SMTP 등)
    # console: 보내지 않고 서버 로그에 인증번호를 찍는다 (개발용. 배포에서는 쓰지 않는다)
    mail_backend: Literal["smtp", "console"] = "console"
    smtp_host: str = ""
    smtp_port: int = 587  # 587: STARTTLS, 465: SSL
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""  # 비우면 smtp_user

    # 통계표 검색의 질문 임베딩 (app/services/vector_search.py)
    # clova: CLOVA Studio v2 / local: bge-m3 직접 실행 / off: 키워드 검색만
    embedding_backend: Literal["clova", "local", "off"] = "clova"
    # 검색 뒤 HCX-007 최종 검토(상위 후보 중 선택·범위 밖 거르기). LLM_PICK=false 로 끈다
    llm_pick: bool = True
    # filter: 범위 밖 거르기만 (후보 교체 없음, 멤버가 안 정해지면 사용자에게 선택지를 준다).
    # pick: 고른 표로 패밀리를 바꾼다. resolve: 멤버 되묻기를 HCX-007이 대신 확정한다.
    # 실험실 §56-4·레포 측정 §66: 최상 설정 위에서 pick 은 팀원셋 −3(형제 멤버로 바꿈).
    # 11/2 통합 개발셋 224문항: filter 168/206, resolve 170/206 (되묻기 적중 둘 다 12/20).
    # 2문항 차이라 임의 선택보다 사용자 선택을 기본으로 한다
    # pick_decided: 되묻기(멤버·지역) 상태가 아닐 때만 고른 표로 바꾼다. Gemma에서 pick은
    # 최종 정답을 올렸지만(11/15 실험실: 표 하나 408문항 323 → 363) 되묻기 상태에서 쓰면
    # 되묻기 적중이 떨어졌다.
    # 비워 두면 통계표 찾기에 쓰는 LLM에 맞춘다 (HCX → filter, Gemma → pick_decided)
    llm_pick_mode: Literal["filter", "pick", "pick_decided", "resolve"] | None = None
    # /ask의 통계표 찾기에서 계획(질문을 지표로 나누기, HCX-007 1회)을 함께 돌린다
    # (app/agent/multi.py). 지표가 여럿이면 지표마다 찾은 표를 응답에 덧붙인다.
    # HCX 한도가 빠듯하면 SEARCH_PLAN=false 로 끈다
    search_plan: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
