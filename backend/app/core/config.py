from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
from typing import Literal

from pydantic import field_validator
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
    # 한 프로세스가 HCX에 동시에 보내는 요청 수 (app/services/hcx_client.py).
    # HCX-007 한도는 분당 60회·60,000토큰인데, 후보 표를 동시에 계산하면 칸 고르기 호출이
    # 한꺼번에 몰려 질문 하나가 한도를 넘었다 (11/18 변형셋 평가). 0이면 제한하지 않는다.
    # 3 (11/18 세진): 칸 고르기 LLM 상한(질문당 3회)과 맞춤
    hcx_max_concurrency: int = 3
    # HCX 호출 하나가 호출 제한이 풀리기를 기다리는 시간의 합 상한 (초, hcx_client.py).
    # 사용자 질문은 짧게 기다리고 넘으면 429 + Retry-After로 알린다.
    # 평가 스크립트(scripts/eval_devset.py)는 길게(90초) 바꾼다
    hcx_retry_budget_seconds: float = 15.0
    # 질문 이해 결과를 서버 메모리에 1시간 남겨, 글자까지 같은 질문이면 HCX를 다시 부르지 않는다
    # (app/services/qa.py UNDERSTAND_CACHE). 평가로 질문 이해 효과를 잴 때는 QUESTION_CACHE=false
    question_cache: bool = True

    # 통계표 찾기(조건 추출·계획·최종 검토)에 쓰는 LLM (app/services/search_llm.py)
    # auto(기본): GEMMA_BASE_URL이 있으면 Gemma 4 31B, 없으면 HCX-007
    # hcx / gemma: 주소와 관계없이 그 모델로 고정 (gemma인데 주소가 없으면 호출이 실패한다)
    # Gemma는 OpenAI 호환 서버(vLLM)의 /chat/completions로 부른다.
    # 수치 답변(qa)과 연속 대화(ask_session)는 이 설정과 관계없이 HCX-007을 쓴다 (Gemma로 재지 않음)
    search_llm: Literal["auto", "hcx", "gemma"] = "auto"
    gemma_base_url: str = ""  # 예: http://gemma-host:8000/v1. 넣으면 통계표 찾기가 Gemma로 간다
    gemma_api_key: str = ""  # 서버가 키를 받지 않으면 비워 둔다
    gemma_model: str = "gemma-4-31b"  # 서버가 띄운 모델 이름
    # 초. 응답 시간 중앙값이 HCX의 약 2배(9초)다. 120초였을 때는 느린 응답을 다 기다린 뒤에야
    # HCX로 넘어가 문항 하나가 수 분씩 걸렸다 (변형셋 v1 최대 403초, 11/18)
    gemma_timeout: float = 30.0

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

    # 관리자 기능(관리자 API, 관리자 화면)을 쓸 수 있는 곳. 쉼표로 IP 또는 대역을 적는다
    # 예: ADMIN_ALLOW_IPS=203.0.113.5,198.51.100.0/24   비워 두면 어디서나 (개발용)
    # 다른 곳에서는 관리자 계정이어도 관리자 API가 403이고, 화면에 관리자 메뉴가 보이지 않는다
    # (app/api/deps.py). 잘못 적으면 서버가 뜨지 않고 어느 값이 틀렸는지 알려 준다
    admin_allow_ips: str = ""

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
    # 표를 고른 뒤 '함께 볼 표'를 묻는다 (Gemma 가 검토에 답했고, 고른 표와 이름이 거의 같은 표가
    # 후보에 있을 때만 호출 1회 추가). '외국인 배우자'처럼 남편 표 · 아내 표로 나뉜 통계를 함께
    # 안내한다. 기본은 끔 (LLM_PICK_TOGETHER=true 로 켠다): 668문항 중 32%에서 호출했지만
    # 고른 표가 바뀐 문항은 0, 표가 덧붙은 문항은 5개뿐이었다 (11/18)
    llm_pick_together: bool = False
    # /ask의 통계표 찾기에서 계획(질문을 지표로 나누기, HCX-007 1회)을 함께 돌린다
    # (app/agent/multi.py). 지표가 여럿이면 지표마다 찾은 표를 응답에 덧붙인다.
    # HCX 한도가 빠듯하면 SEARCH_PLAN=false 로 끈다
    search_plan: bool = True
    # 표에서 바로 계산하지 못했을 때, 가까운 연도 · 다른 표로 계산한 대안을 LLM 1회가 골라 값과 함께
    # 되묻는다 (app/services/alternative.py). 대안이 계산되지 않으면 LLM을 부르지 않는다.
    # 기본은 끔 (HANDOFF_ALTERNATIVES=true 로 켠다). 11/18 변형셋 · 개발셋에서 대안 되묻기 13건 중
    # 9건이 조건을 바꾸지 않은 정답을 "조건을 모두 만족하지 못함"으로 되물어 불필요한 되묻기가
    # 됐고, 미리 계산한 값이 result · sources 밖(대화 상태)에만 있어 환각 숫자 16 · 17건이 모두
    # 여기서 나왔다. 나머지는 최신 자료가 있는데 옛 연도를 내놓은 대안이었다
    handoff_alternatives: bool = False
    # 넘기기 꾸러미의 후보 표에서 규칙(글자 맞추기)이 칸을 못 고르면 LLM 1회가 칸을 고른다
    # (app/services/cell_pick.py, Gemma 기본). HANDOFF_CELL_LLM=false 로 끄면 지금처럼 계산하지 않음
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
