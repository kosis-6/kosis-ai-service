# kosis-ai-service

[KOSIS 국가통계포털](https://kosis.kr) 데이터를 AI로 검색·분석하는 서비스입니다.

> 프로젝트 초기 단계입니다. 아래 내용은 구현이 진행되면서 채워 나갈 예정입니다.

- 연구계획서: [Notion](https://app.notion.com/p/likelion/40244860a4f4801b81f3d1b3c1ba1ed3)

## 프로젝트 구성

| 구분 | 프로젝트 | 핵심 목표 |
| --- | --- | --- |
| 실전 프로젝트 1 | 통계 수치 요약 서비스 | 통계 수치 분석 및 자연어 요약 |
| 실전 프로젝트 2 | 통계표 안내 챗봇 | 사용자 질의에 적합한 통계표 탐색 및 경로 안내 |
| 종합 프로젝트 | 맞춤형 통계 AI 챗봇 | 통계표 탐색부터 수치 분석·시각화까지 통합 |

## 구성원

| 이름 | 역할 |
| --- | --- |
| 이정제 (팀장)| ML옵스 및 AI 엔지니어링|
| 윤지연 | 평가 엔지니어링 |
| 고은성 | 데이터 엔지니어링 |

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
KOSIS_API_KEY=내가_발급받은_KOSIS_인증키
HCX_API_KEY=팀장(이정제)에게_전달받은_키
POSTGRES_PASSWORD=내가_정한_DB_비밀번호
```

| 변수 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `KOSIS_API_KEY` | O | (없음) | KOSIS OpenAPI 인증키 (각자 발급하고 공유하지 않음) |
| `HCX_API_KEY` | O | (없음) | HyperCLOVA X (CLOVA Studio) API 키 (팀장에게 전달받음) |
| `POSTGRES_PASSWORD` | O | (없음) | 내 PC에 생성되는 DB 비밀번호 (각자 정하고 공유하지 않음) |
| `APP_NAME` | | `kosis-ai-service` | 앱 이름 |
| `DEBUG` | | `false` | 디버그 모드 |

**KOSIS 인증키 발급 방법**

1. [KOSIS 공유서비스](https://kosis.kr/openapi/)에 회원가입 후 로그인합니다.
2. 상단 메뉴의 **활용신청**에서 인증키를 신청합니다. (무료)
3. 발급된 인증키를 복사해 `.env`의 `KOSIS_API_KEY`에 넣습니다.

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

## 데이터 수집

KOSIS OpenAPI로 통계표 수치를 받아 `data/` 폴더에 저장합니다.
**데이터 파일은 GitHub에 올리지 않습니다.** (`.gitignore`에 등록되어 있음) 각자 아래 명령어로 받습니다.

### 받을 표 목록

[`scripts/tables.csv`](scripts/tables.csv)에 주제별로 정리되어 있습니다. 표를 추가하려면 이 파일에 한 줄을 추가합니다.

| 열 | 설명 | 예시 |
| --- | --- | --- |
| `org_id` | 기관 ID | `101` (국가데이터처) |
| `tbl_id` | 통계표 ID | `DT_1B81A21` |
| `topic` | 주제 | 출산 |
| `tbl_nm` | 표 이름 (참고용) | 시도/합계출산율 모의 연령별 출산율 |

> 표 ID와 기관 ID는 KOSIS 통계표 화면 주소의 `tblId=`, `orgId=` 값에서 확인할 수 있습니다.

### 실행 방법

컨테이너 터미널에서 실행합니다.

```bash
# 목록 전체 받기 (약 10분)
python -m scripts.collect_kosis --list scripts/tables.csv

# 표 하나만 받기
python -m scripts.collect_kosis 101 DT_1B81A21

# 실제로 받지 않고 요청 계획만 확인
python -m scripts.collect_kosis --list scripts/tables.csv --dry-run
```

- KOSIS는 1회 요청에 40,000셀까지만 주기 때문에, 큰 표는 자동으로 나눠서 요청합니다.
- 분당 호출 제한이 있어 요청 사이에 1초씩 쉽니다.

### 결과 파일

| 경로 | 내용 |
| --- | --- |
| `data/processed/<표ID>.csv` | 수집한 데이터 (값 하나당 한 줄) |
| `data/processed/<표ID>.meta.json` | 표 이름, 분류 이름, 항목 목록, 수록 기간 |
| `data/raw/<표ID>/` | API 원본 응답 (`--save-raw`를 붙였을 때만 저장, 용량이 커서 기본은 저장 안 함) |

CSV 주요 열:

| 열 | 설명 | 예시 |
| --- | --- | --- |
| `period` | 시점 | `2025` |
| `item_nm` | 항목 | 합계출산율 |
| `c1_nm`, `c2_nm` ... | 분류값 (표마다 의미가 다름, `meta.json`의 `classes` 참고) | 서울특별시 |
| `c1_level` | 분류 단계 (1 = 전국·시도, 2 = 시군구 등) | `1` |
| `c1_path` | 상위 분류까지 이어 붙인 경로 | 서울특별시 > 종로구 |
| `value` | 수치 (숫자가 아니면 비어 있음) | `0.632` |
| `value_flag` | 숫자 대신 온 통계부호 (`-` 해당 없음, `X` 비밀보호 등) | `X` |
| `unit` | 단위 | 명 |
| `last_changed` | KOSIS 최종 수정일 | `2026-08-12` |

> **주의: 아직 정제 전 데이터입니다.**
> - 전국·시도·시군구 행과 "계"·세부 행이 함께 들어 있어, 조건 없이 더하면 **중복 집계**됩니다.
> - `value_flag`가 있는 칸은 0이 아니라 **결측**으로 다룹니다.
> - 표마다 기준(예: 주민등록인구는 외국인 제외)이 다르므로 답변에 출처를 함께 표시합니다.

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
├── scripts/
│   ├── collect_kosis.py     # KOSIS 통계표 수집
│   └── tables.csv           # 수집할 통계표 목록
├── data/                    # 수집한 데이터 (각자 생성, 커밋 X)
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