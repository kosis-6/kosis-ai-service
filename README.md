# kosis-ai-service

[KOSIS 국가통계포털](https://kosis.kr) 데이터를 AI로 검색·분석하는 서비스입니다.

> 프로젝트 초기 단계입니다. 아래 내용은 구현이 진행되면서 채워 나갈 예정입니다.

## 프로젝트 구성

| 구분 | 프로젝트 | 핵심 목표 |
| --- | --- | --- |
| 실전 프로젝트 1 | 통계 수치 요약 서비스 | 통계 수치 분석 및 자연어 요약 |
| 실전 프로젝트 2 | 통계표 안내 챗봇 | 사용자 질의에 적합한 통계표 탐색 및 경로 안내 |
| 종합 프로젝트 | 맞춤형 통계 AI 챗봇 | 통계표 탐색부터 수치 분석·시각화까지 통합 |

## 구성원

| 이름 | 역할 |
| --- | --- |
| 문현욱 | AI 엔지니어링 |
| 윤지연 | 평가 엔지니어링 |
| 고은성 | 데이터 엔지니어링 |
| 이정제 (팀장)| ML옵스 및 각 파트 지원|

## 기술 스택

- 언어 / 프레임워크: Python 3.12.15, FastAPI
- 패키지 관리: uv 0.13.3
- DB: PostgreSQL 17 + pgvector 0.8.7 (벡터 검색), pg_trgm (키워드 검색 보완)
- 실행 환경: Docker, Docker Compose
- 테스트 / 린트: pytest, ruff

## 개발 환경 세팅

Mac / Windows 모두 **Docker로 동일한 환경**에서 실행합니다.

### 1. 사전 준비

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) 설치 후 실행
- Git
- [VS Code](https://code.visualstudio.com/) + [Dev Containers 확장](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

### 2. 프로젝트 받기

```bash
git clone <저장소 주소>
cd kosis-ai-service
```

### 3. 환경변수 파일 만들기

프로젝트 최상위 폴더에 `.env` 파일을 직접 만들고 아래 내용을 입력합니다.

```
KOSIS_API_KEY=팀장(이정제)에게_전달받은_키
```

| 변수 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `KOSIS_API_KEY` | O | (없음) | KOSIS OpenAPI 인증키 |
| `APP_NAME` | | `kosis-ai-service` | 앱 이름 |
| `DEBUG` | | `false` | 디버그 모드 |
| `POSTGRES_USER` | | `kosis` | DB 사용자 |
| `POSTGRES_PASSWORD` | | `kosis` | DB 비밀번호 |
| `POSTGRES_DB` | | `kosis` | DB 이름 |
| `POSTGRES_HOST_PORT` | | `5432` | 내 PC에서 DB에 접속할 포트 (이미 5432를 쓰고 있으면 변경) |

> - `.env`는 **절대 커밋하지 않습니다.** (`.gitignore`에 등록되어 있음)
> - 새 환경변수를 추가하면 위 표에 이름과 설명을 추가하고, 실제 값은 팀원에게 직접 공유해 주세요.

### 4. 컨테이너로 들어가 개발하기

1. VS Code에서 프로젝트 폴더를 엽니다.
2. 오른쪽 아래 **"Reopen in Container"** 알림을 클릭합니다.
   (알림이 안 보이면 `Ctrl/Cmd + Shift + P` → `Dev Containers: Reopen in Container`)
3. 처음에는 이미지 빌드로 몇 분 걸립니다. 완료되면 VS Code가 컨테이너 안에서 열립니다.

컨테이너가 열리면 아래가 **자동으로** 준비됩니다.

- Python 3.12.15 + 프로젝트 의존성
- Python, Ruff 확장 설치 및 인터프리터 설정 (저장 시 자동 포맷)
- FastAPI 서버 실행 → http://localhost:8000/docs 에서 API 문서 확인
- PostgreSQL(pgvector) 실행 → http://localhost:8000/health/db 에서 연결 확인

VS Code 터미널도 컨테이너 안에서 열리므로 `pytest` 등 명령어를 바로 실행할 수 있습니다.
코드를 수정하면 서버가 자동으로 재시작됩니다.

## 프로젝트 구조

```
kosis-ai-service/
├── app/
│   ├── main.py              # FastAPI 앱 생성, 라우터 등록 (진입점)
│   ├── core/
│   │   ├── config.py        # 환경변수(.env) 설정 관리
│   │   └── db.py            # DB 연결 (SQLAlchemy 비동기 세션)
│   ├── api/
│   │   ├── router.py        # 모든 라우터를 하나로 모음
│   │   └── routes/          # 기능별 API 엔드포인트
│   │       └── health.py
│   └── services/            # 비즈니스 로직 (KOSIS 호출, AI 처리 등)
│       └── kosis_client.py
├── tests/                   # pytest 테스트
├── docker/postgres/init.sql # DB 최초 생성 시 확장(pgvector, pg_trgm) 설치
├── .devcontainer/           # VS Code 컨테이너 개발 환경 설정
├── Dockerfile               # 이미지 정의 (Python 3.12.15 + uv)
├── compose.yaml             # 로컬 개발용 실행 설정 (api + db)
├── pyproject.toml           # 의존성 및 도구 설정
├── uv.lock                  # 의존성 정확한 버전 고정
└── .env                     # 환경변수 (각자 생성, 커밋 X)
```

### 요청 처리 흐름

```
요청 → main.py → api/router.py → api/routes/*.py → services/*.py → 응답
```

| 계층 | 역할 | 예시 |
| --- | --- | --- |
| `routes/` | 요청을 받고 입력값 검증 후 응답 반환 (입구 역할만) | `POST /summary` |
| `services/` | 실제 로직 처리 | KOSIS API 호출, LLM 요약 |
| `core/` | 앱 전체 공통 설정 | API 키, 디버그 모드, DB 연결 |

### 새 API 추가하기

1. `app/api/routes/`에 파일 추가 (예: `summary.py`)

   ```python
   from fastapi import APIRouter

   router = APIRouter(prefix="/summary", tags=["summary"])


   @router.post("")
   async def summarize() -> dict[str, str]:
       return {"summary": "..."}
   ```

2. `app/api/router.py`에 등록

   ```python
   from app.api.routes import health, summary

   api_router.include_router(summary.router)
   ```

3. 로직은 `app/services/`에 작성하고 route에서 호출합니다.
4. `tests/`에 테스트를 추가합니다.