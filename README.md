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
- DB: DuckDB 2.0 (통계 데이터), PostgreSQL 17 + pgvector 0.8.7, pg_trgm (서비스 데이터)
- 실행 환경: Docker, Docker Compose
- 테스트 / 린트: pytest, ruff

### DB 역할

| DB | 담는 것 | 읽기·쓰기 | 만드는 방법 |
| --- | --- | --- | --- |
| DuckDB (`data/processed/kosis.duckdb`) | 전처리한 통계 수치, 지역, 통계표 카탈로그 | 서버는 읽기만 | `python pipeline/run_all.py` |
| PostgreSQL (`compose.yaml`의 `db`) | 통계표 검색용 임베딩(pgvector)·키워드 검색(pg_trgm), 대화 기록, 질의·평가 로그 | 서버가 읽고 씀 | `docker compose up` 시 자동 생성 |

- 통계 수치는 DuckDB에서만 조회합니다. PostgreSQL에 복사하지 않습니다.
- PostgreSQL은 실전 2(통계표 안내 챗봇)부터 사용합니다. 지금은 연결 확인(`/health/db`)만 있습니다.
- DuckDB는 한 번에 한 프로세스만 쓸 수 있어서, 요청마다 저장하는 데이터는 PostgreSQL에 둡니다.

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
# 목록 전체 받기 (20개 표 약 15분, 주민등록인구 DT_1B04006만 약 7분)
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
| `data/processed/<표ID>.meta.json` | 표 이름, 분류 이름, 항목 목록, 수록 기간, 수집 시각(`collected_at`) |
| `data/raw/<표ID>/` | API 원본 응답 (`--save-raw`를 붙였을 때만 저장, 용량이 커서 기본은 저장 안 함) |

수집이 끝나면 표별 수록 기간·행 수·수집 시각을 한 표로 정리해 보고서에 붙일 수 있습니다. `meta.json`이 없는 표는 '미수집'으로 표시됩니다.

```bash
python -m scripts.collect_report   # → data/reports/collect_<시각>.md (보고서용) / .csv (엑셀용)
```

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

## 질의응답

### 1. 전처리 DB 만들기

팀에서 공유받은 원본 zip을 `data/kosis_processed.zip`에 두고 컨테이너 터미널에서 실행합니다.

```bash
python pipeline/run_all.py   # 압축 해제 → 전처리 → 카탈로그 → 검증 → DB 교체
```

`data/processed/kosis.duckdb`가 만들어집니다. 테이블과 규칙은 [`docs/data_dictionary.md`](docs/data_dictionary.md)를 참고합니다.

KOSIS에서 직접 다시 받아 DB를 갱신할 때는 zip 없이 수집 결과를 바로 씁니다.

```bash
python -m scripts.collect_kosis --list scripts/tables.csv   # KOSIS OpenAPI 수집
python pipeline/run_all.py --source collect                 # 수집 결과 → 전처리 → 검증 → DB 교체
```

- DB는 임시 파일(`kosis.new.duckdb`)에 먼저 만들고, **검증을 모두 통과해야** `kosis.duckdb`로 바꿉니다. 중간에 실패하면 기존 DB가 그대로 남아 서비스가 계속 동작합니다.
- 실행 중인 서버는 기존 DB를 계속 읽으므로, 교체 후 `docker compose restart api`로 반영합니다.
- 표마다 수집 시각은 `catalog.collected_at`에 들어갑니다 (예전 zip 자료는 기록이 없어 비어 있음).

### 2. 질문하기

```bash
# API (서버 실행 중) → http://localhost:8000/docs 에서도 시험 가능
curl -X POST localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question": "2025년 서울과 부산 중 고령인구 비율이 높은 지역은?"}'

# 명령줄 (중간 결과까지 보려면 --debug)
python -m scripts.ask -q "최근 5년 부산 인구 변화" --debug
```

처리 순서 (`app/services/qa.py`):

| 단계 | 담당 | 내용 |
| --- | --- | --- |
| 1. 질문 이해 | HCX-007 | 질문 → 유형·지표·지역·연도 JSON |
| 2~4. 판단·조회·계산 | 코드 (`indicators.py`) | 답변 가능 여부(status) 판단, DuckDB 조회, 계산 |
| 5. 답변 생성 | 코드 (`qa.py`) | 질문 유형별 템플릿에 계산 결과를 채워 문장 작성, 수치 목록·출처·KOSIS 통계표 링크 첨부 |

응답의 `status`는 `ok` 또는 답변 불가 사유이고, `sources`에 답변에 쓴 통계표가 들어 있습니다. 답변 불가일 때는 사유와 다시 질문하는 방법을 안내합니다.

| 경우 | `status` | 처리 |
| --- | --- | --- |
| 인구 통계와 관계없는 질문 | `not_supported_question` | 거절 + 지원 지표 목록 |
| 지원하지 않는 지표 | `unknown_indicator` | 거절 + 지원 지표 목록 |
| 지역 누락 | `missing_region` | 지역을 넣어 다시 질문 요청 (순위 질문은 지역 없이 답함) |
| 모르는 지역 이름 | `unknown_region` | 시도·시군구 이름으로 다시 질문 요청 |
| 같은 이름의 여러 지역 (예: 중구) | `ambiguous_region` | 후보 목록을 보여 주고 다시 질문 요청 |
| 수록 기간 밖 연도 | `out_of_range` | 수록 기간 안내. 미래 연도이고 장래추계가 있으면 추계값을 대신 보여줌 (status는 그대로) |
| 그 연도에 없던 지역 (예: 2020년 대구 군위군) | `region_not_in_year` | 지역이 있던 기간 안내 |
| 수록 기간 안이지만 값이 빔 | `missing_data` | 다른 연도·지역으로 질문 요청 |
| 질문 해석 실패 | `parse_error` | 지역·연도·지표를 넣어 다시 질문 요청 |
| 원인·정책 효과 질문 | `ok` | 원인은 판단하지 않고 수치 변화만 답함 |

답변 문장은 `qa.py`의 템플릿으로 만듭니다: 요약 문장 `summary()`, 수치 목록·출처 `render()`, 답변 불가 안내 `REFUSAL_GUIDE`.

질문할 때는 KOSIS를 호출하지 않습니다. DB에 없는 데이터 때문에 답하지 못한 질문(`unknown_indicator`, `out_of_range`, `region_not_in_year`, `missing_data`)은 `data/requests/missing.jsonl`에 쌓이고, 다음 회차 수집 대상을 정할 때 집계해서 봅니다.

```bash
python -m scripts.missing_requests   # 같은 요청끼리 묶어 많이 나온 순서로
```

모든 `/ask` 요청은 `data/requests/requests.jsonl`에 HCX 원문, 해석 결과(코드 보정 내역 포함), 계산 결과, 단계별 응답 시간과 함께 남습니다. 틀린 답이 질문 해석 문제인지 조회·계산 문제인지 여기서 추적합니다.

```bash
python -m scripts.request_log                       # status별 건수·평균 응답 시간 + 최근 10건
python -m scripts.request_log --status parse_error  # 이 status만
python -m scripts.request_log --last 3 --detail     # HCX 원문·해석·계산 결과까지
```

### 3. 골든셋 평가

`eval/golden.csv`의 질문을 서비스에 넣고 4개 영역을 자동 채점합니다 (질문마다 HCX 1회).

```bash
python -m scripts.eval_golden                  # 전체
python -m scripts.eval_golden --only g01 g11   # 일부 질문만
python -m scripts.eval_golden --round 3 --note "남녀 비중 보정 추가"   # 회차 기록
```

| 영역 | 채점 방법 |
| --- | --- |
| 질의 이해 | HCX 해석(`parsed`)을 정답 칸(`type`, `indicator`, `regions`, `year` ...)과 비교 |
| 데이터 정확성 | 계산 결과의 `target` 값(`value`, `rate`, `ranking`, `share:여자` 등)을 `expected`와 비교. 숫자는 정답에 적은 자릿수만큼 비교 |
| 답변 신뢰성 | 답한 경우 출처·KOSIS 통계표 링크, 답변 불가인 경우 안내 문장이 있는지 |
| 답변 불가 판단 | `status`를 `expected_status`와 비교 (거절 여부, 사유 코드) |
| 자연어 품질 | 자동 채점 안 함. 결과 CSV의 `quality` 칸에 사람이 0~2점 |

- 결과는 `data/eval/golden_<시각>.csv`(질문별, 엑셀용)와 `.md`(영역별 요약, 틀린 질문 목록)에 저장됩니다.
- `.md` 머리에 회차, 변경 내용, 채점에 쓴 DB(생성·최근 수집 시각)가 남아, 정확도가 바뀐 이유가 코드인지 데이터인지 구분할 수 있습니다.
- 질문을 추가할 때는 빈 칸은 채점하지 않으므로 아는 칸만 채웁니다. `expected`는 KOSIS 화면 등에서 확인한 값을 넣고 `expected_source`에 확인한 곳을 적습니다.
- 같은 질문도 실행마다 HCX 해석이 달라질 수 있어, 정확도는 여러 번 돌려 비교합니다.

## 프로젝트 구조

```
kosis-ai-service/
├── app/
│   ├── main.py              # FastAPI 앱 생성, 라우터 등록 (진입점)
│   ├── core/
│   │   ├── config.py        # 환경변수(.env) 설정 관리
│   │   ├── db.py            # PostgreSQL 연결 (SQLAlchemy 비동기 세션)
│   │   └── duckdb.py        # 전처리 DB(kosis.duckdb) 읽기 전용 연결
│   ├── api/
│   │   ├── router.py        # 모든 라우터를 하나로 모음
│   │   └── routes/          # 기능별 API 엔드포인트
│   │       ├── health.py
│   │       └── ask.py       # POST /ask 질의응답
│   ├── schemas/             # 요청·응답 모델 (Pydantic)
│   ├── prompts/             # LLM 프롬프트, 답변 정책(answer_policy.md)
│   └── services/            # 비즈니스 로직
│       ├── kosis_client.py  # KOSIS OpenAPI 호출
│       ├── hcx_client.py    # HCX(CLOVA Studio) 호출
│       ├── analysis.py      # 증감률·구성비·순위 등 계산 함수
│       ├── indicators.py    # 지표 조회·추이·비교·순위 (DuckDB)
│       ├── catalog.py       # 통계표 설명·비교 가능 여부 조회
│       └── qa.py            # 질문 → 답변 (1~5단계 연결)
├── pipeline/                # 전처리 DB를 만드는 배치 작업 (서버와 별개)
├── config/catalog_notes.toml # 통계표 설명·지표 사전 원본
├── docs/                    # 데이터 사전, 작업 기록
├── scripts/
│   ├── collect_kosis.py     # KOSIS 통계표 수집
│   ├── collect_report.py    # 수집 결과 표 (표별 수록 기간·행 수·수집 시각)
│   ├── tables.csv           # 수집할 통계표 목록
│   ├── check_hcx.py         # HCX 연결 확인
│   ├── ask.py               # 질의응답 명령줄 실행
│   ├── eval_golden.py       # 골든셋 자동 평가
│   ├── compare_llm.py       # 전처리 데이터 유무에 따른 HCX 답변 비교 실험
│   ├── clova_eval.py        # 계산을 코드 대신 CLOVA가 할 때 정확도 비교 실험
│   ├── missing_requests.py  # DB에 없는 데이터 요청 집계
│   └── request_log.py       # 요청 로그 보기 (원인 추적)
├── eval/golden.csv          # 골든셋 (질문 + 정답)
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