# kosis-ai-service

[KOSIS 국가통계포털](https://kosis.kr) 데이터를 AI로 검색·분석하는 서비스입니다. 실전 프로젝트 1·2를 마쳤습니다.

- 연구계획서: [Notion](https://app.notion.com/p/likelion/40244860a4f4801b81f3d1b3c1ba1ed3)

## 프로젝트 구성

| 구분 | 프로젝트 | 핵심 목표 | API |
| --- | --- | --- | --- |
| 실전 프로젝트 1 | 통계 수치 요약 서비스 | 통계 수치 분석 및 자연어 요약 | `POST /ask` |
| 실전 프로젝트 2 | 통계표 안내 챗봇 | 사용자 질의에 적합한 통계표 탐색 및 경로 안내 | `POST /search`, 화면 `/chat` |
| 종합 프로젝트 | 맞춤형 통계 AI 챗봇 | 통계표 탐색부터 수치 분석·시각화까지 통합 | |

## 구성원

| 이름 | 역할 | 주요 작업 |
| --- | --- | --- |
| 이정제 (팀장) | AI 엔지니어링, MLOps | 개발 환경·DB, 수치 계산·질의응답 API, 슬롯·검색·되묻기 파이프라인, 프론트 UI |
| 고은성 | 데이터 엔지니어링 | 통계표 메타데이터 정제, 패밀리 묶기, 분류 라벨 보정, 되묻기 후보·시나리오 |
| 윤지연 | 평가 엔지니어링 | 전처리 파이프라인, 골든셋 평가, pandas 정답 채점, 재작성 비교 측정, 첫 채팅 화면 |
| 최세진 | AI 엔지니어링, 평가·실패 분석 | HCX-007 최종 검토, 쌍곡 어댑터, 상대시점·연령 해석, 일상어 사전, 오답 분석 |

## 기술 스택

- 언어 / 프레임워크: Python 3.12.15, FastAPI
- 패키지 관리: uv 0.13.3
- LLM / 임베딩: HyperCLOVA X(HCX-007, HCX-DASH-002), CLOVA Studio 임베딩 v2(bge-m3)
- DB: DuckDB 2.0 (통계 데이터), PostgreSQL 17 + pgvector 0.8.7 (서비스 데이터)
- 실행 환경: Docker, Docker Compose
- 테스트 / 린트: pytest, ruff

| DB | 담는 것 | 읽기·쓰기 | 만드는 방법 |
| --- | --- | --- | --- |
| DuckDB (`data/processed/kosis.duckdb`) | 전처리한 통계 수치, 지역, 통계표 카탈로그 | 서버는 읽기만 | `python pipeline/run_all.py` |
| PostgreSQL (`compose.yaml`의 `db`) | 표 메타데이터(`stat_tables`), 패밀리(`table_families`), 표 임베딩(`table_embeddings`), 대화 상태(`chat_sessions`), 요청 로그(`search_logs`) | 서버가 읽고 씀 | `docker compose up` 시 자동 생성, 검색 데이터는 [아래](#1-검색용-데이터-적재)에서 적재 |

- 통계 수치는 DuckDB에서만 조회합니다. DuckDB는 한 번에 한 프로세스만 쓸 수 있어서, 요청마다 저장하는 데이터는 PostgreSQL에 둡니다.
- `pg_trgm` 확장은 설치되어 있지만 검색에는 쓰지 않습니다. 키워드 점수는 서버 메모리에 올린 표 정보로 계산합니다.

## 개발 환경 세팅

> 레포는 `frontend/`(화면)와 `backend/`(서버·파이프라인·데이터)로 나뉩니다. **이 문서의 `app/`, `pipeline/`, `scripts/`, `data/`, `eval/`, `config/`, `tests/` 경로는 모두 `backend/` 기준**이고, 명령어는 컨테이너 터미널(`/workspace/backend`)에서 실행합니다. `docker compose`는 레포 최상위에서 실행합니다.

### 1. 사전 준비

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) 설치 후 실행
- Git
- [VS Code](https://code.visualstudio.com/) + [Dev Containers 확장](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

### 2. 환경변수 파일 만들기

프로젝트 최상위 폴더에 `.env` 파일을 만듭니다. `.env`는 **커밋하지 않습니다.**

```
KOSIS_API_KEY=내가_발급받은_KOSIS_인증키
HCX_API_KEY=팀장(이정제)에게_전달받은_키
POSTGRES_PASSWORD=내가_정한_DB_비밀번호
```

| 변수 | 필수 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `KOSIS_API_KEY` | O | (없음) | KOSIS OpenAPI 인증키. [KOSIS 공유서비스](https://kosis.kr/openapi/)의 **활용신청**에서 무료로 발급 |
| `HCX_API_KEY` | O | (없음) | HyperCLOVA X (CLOVA Studio) API 키 |
| `POSTGRES_PASSWORD` | O | (없음) | 내 PC에 생성되는 DB 비밀번호 |
| `POSTGRES_HOST_PORT` | | `5432` | 내 PC에서 DB에 접속하는 포트 (이미 쓰고 있으면 변경) |
| `EMBEDDING_BACKEND` | | `clova` | 질문 임베딩: `clova` / `local`(bge-m3 직접 실행) / `off`(키워드만) |
| `LLM_PICK` | | `true` | 검색 뒤 HCX-007 최종 검토 |
| `LLM_PICK_MODE` | | `filter` | `filter`(범위 밖 거르기만) / `pick`(고른 표로 교체) / `resolve`(멤버 되묻기를 HCX가 확정) |

### 3. 컨테이너로 들어가기

1. VS Code에서 프로젝트 폴더를 열고 **"Reopen in Container"** 를 누릅니다. (알림이 안 보이면 `Ctrl/Cmd + Shift + P` → `Dev Containers: Reopen in Container`)
2. 처음에는 이미지 빌드로 몇 분 걸립니다. 서버와 DB가 함께 뜨고, 코드를 고치면 서버가 자동으로 재시작됩니다.

| 주소 | 내용 |
| --- | --- |
| http://localhost:8000/chat | 통계표 안내 채팅 화면 (`?mock=1` 서버·HCX 없이 목업, `?debug=1` 검색 과정 표시) |
| http://localhost:8000/docs | API 문서 |
| http://localhost:8000/health/db | DB 연결 확인 |
| http://localhost:8000/health/search | 검색 데이터 준비 상태 (`tables` 2364, `retrieval` `hybrid`면 정상) |

### 4. 데이터 준비

**데이터 파일은 GitHub에 올리지 않습니다.** 실전 1은 [전처리 DB 만들기](#1-전처리-db-만들기), 실전 2는 [검색용 데이터 적재](#1-검색용-데이터-적재)를 실행합니다.

### 테스트

```bash
pytest
ruff check .
```

## 통계표 안내 (실전 2)

질문에 맞는 KOSIS 통계표를 찾아 링크로 안내합니다. 수치는 생성하지 않고, 표를 찾지 못하면 범위 밖으로 안내합니다.

| 단계 | 담당 | 내용 |
| --- | --- | --- |
| 1. 질의 분석 | HCX-DASH-002 + 코드 (`slots.py`) | 질문 → 주제·대상·연도·지역 수준·성별 슬롯, 코드 보정 |
| 2. 후보 검색 | 코드 (`search.py`, `synonyms.py`, `vector_search.py`) | 임베딩 점수 + 키워드 점수(일상어 사전으로 확장) |
| 3. 조건 검증·재순위 | 코드 (`search.py`) | 표마다 연도·지역 수준·성별·분류·대상을 판정해 가감점 |
| 4. 패밀리 묶기 | 코드 (`search.py`) | 대상·성별·연도 판만 다른 표를 한 자리로 묶음 |
| 5. 멤버·판 선택 | 코드 (`search.py`, `dialog.py`) | 대상·성별·연도로 표를 정하고, 정해지지 않으면 되묻기 |
| 6. 최종 검토 | HCX-007 (`llm_pick.py`) | 상위 후보 중 질문 주제를 다루는 표가 없으면 범위 밖으로 판정 |
| 7. 응답 | 코드 (`search_answer.py`) | 템플릿으로 안내 문장·링크 작성 |

### 1. 검색용 데이터 적재

처음 한 번, 메타데이터가 바뀌면 다시 실행합니다. `data/catalog/population_table_catalog_with_meta.jsonl`이 있어야 합니다.

```bash
# 1) 표 임베딩 계산 (내 PC에서, 레포 최상위 기준) → data/embeddings/bge-m3__full.npy
cd backend
uv run --with sentence-transformers python -m pipeline.build_embeddings --dry-run
cd ..
# 2) stat_tables → table_families → table_embeddings 한 번에 적재
docker compose exec api python -m pipeline.build_search
# 3) 서버 재시작 (표 정보는 서버가 처음 한 번 읽어 둠)
docker compose restart api
```

- 1)은 처음에 bge-m3 모델(약 2GB)을 내려받습니다. 어렵다면 2)에 `--model clova-v2`를 붙여 서버에서 CLOVA로 만듭니다 (약 40분, HCX 키 사용량 발생).
- 패밀리 규칙만 바뀌었으면 `pipeline.build_search --skip-catalog`로 카탈로그 적재를 건너뜁니다.
- Windows는 WSL2(Ubuntu) 터미널에서 실행합니다.

### 2. 질문하기

```bash
curl -s -X POST localhost:8000/search -H 'Content-Type: application/json' \
  -d '{"question": "앞으로 인구가 어떻게 될지 예측", "debug": true}'
python -m app.services.search "2024년 부산 시군구별 혼인 귀화자 수"   # 명령줄
```

되묻기에 답할 때는 응답의 `session_id`를 다시 보냅니다. `debug: true`면 후보별 점수와 슬롯 보정 내역이 함께 나오고, `debug.retrieval`이 `keyword`면 표 임베딩이 없거나 질문 임베딩이 실패한 것입니다.

| `status` | 뜻 |
| --- | --- |
| `ok` | 표를 찾음 |
| `partial_match` | 표를 찾았지만 질문 조건 일부가 맞지 않아 함께 고지 |
| `clarify` | 대상·성별·연도를 정해야 해서 되물음 |
| `ambiguous` | 후보 패밀리 점수가 비슷해 선택지를 보여 줌 |
| `out_of_scope` | 범위 안에서 맞는 표를 찾지 못함 |
| `not_statistics` | 통계표를 찾는 질문이 아님 (`LLM_PICK=false`일 때만. 기본 설정에서는 `out_of_scope`) |
| `parse_error` | 질문을 이해하지 못함 |

### 3. 요청 로그 보기

틀린 안내가 슬롯 / 검색 / 재순위 / 패밀리·판 선택 중 어디서 생겼는지 가를 때 씁니다.

```bash
docker compose exec api python -m scripts.search_log                     # 상태별 요약 + 최근 10건
docker compose exec api python -m scripts.search_log --last 3 --detail   # 후보 10개·HCX 원문·답변까지
docker compose exec api python -m scripts.search_log --session S_abc123  # 한 대화의 턴 순서대로
```

### 4. 평가와 결과

개발셋은 `data/팀장전달_2026-11-02_개발셋통합/dev_all.jsonl` 224문항입니다 (범위 안 206, 범위 밖 18, 팀 공유).

```bash
python -m scripts.family_ablation --golden data/팀장전달_2026-11-02_개발셋통합/dev_all.jsonl --llm-pick   # 패밀리 켬/끔 비교
python -m scripts.check_search              # 일상어 질문으로 키워드 vs 하이브리드 점검 (골든셋 아님)
python -m scripts.check_clarify_scenarios   # 되묻기 대화 시나리오 채점
```

| 지표 (11/3, HCX 슬롯 + LLM 검토) | 결과 |
| --- | --- |
| 표 R@1 / R@5 | 82% (168/206) / 93% (192/206) |
| 흐름 성공 (되묻기까지 거쳐 정답 표 도달) | 84% (174/206) |
| 되묻기 문항 성공 | 65% (13/20) |
| 범위 밖 재현율 / 범위 안→밖 오판 | 94% (17/18) / 4건 |

- 기록: `eval/results/family_ablation_20261103.md`
- 오답을 보고 사전·규칙을 고친 개발셋 결과라, 처음 보는 질문에 대한 성능이 아닙니다.
- 위 수치는 일상어 사전 수정(원문에서 찾기) 전입니다. 수정 전후를 HCX 없이 규칙 슬롯으로 비교하면 흐름 성공이 177 → 182/206이고, HCX 슬롯 + LLM 검토 기준으로는 다시 재지 않았습니다.

## 수치 요약 (실전 1)

### 1. 전처리 DB 만들기

팀에서 공유받은 원본 zip을 `data/kosis_processed.zip`에 두고 실행합니다. 테이블과 규칙은 [`docs/data_dictionary.md`](docs/data_dictionary.md)에 있습니다.

```bash
python pipeline/run_all.py   # 압축 해제 → 전처리 → 카탈로그 → 검증 → DB 교체
```

KOSIS에서 직접 다시 받을 때는 zip 없이 수집 결과를 씁니다. 받을 표 목록은 `scripts/tables.csv`입니다 (20개 표 약 15분).

```bash
python -m scripts.collect_kosis --list scripts/tables.csv   # KOSIS OpenAPI 수집
python pipeline/run_all.py --source collect                 # 수집 결과 → 전처리 → 검증 → DB 교체
```

DB는 임시 파일에 먼저 만들고 **검증을 모두 통과해야** 교체합니다. 교체 후에는 `docker compose restart api`로 반영합니다.

### 2. 질문하기

```bash
curl -X POST localhost:8000/ask -H 'Content-Type: application/json' \
  -d '{"question": "2025년 서울과 부산 중 고령인구 비율이 높은 지역은?"}'
python -m scripts.ask -q "최근 5년 부산 인구 변화" --debug   # 명령줄
```

| 단계 | 담당 | 내용 |
| --- | --- | --- |
| 1. 질문 이해 | HCX-007 | 질문 → 유형·지표·지역·연도 JSON |
| 2~4. 판단·조회·계산 | 코드 (`indicators.py`, `lookup.py`) | 답변 가능 여부 판단, DuckDB 조회, 계산 |
| 5. 답변 생성 | 코드 (`qa.py`) | 템플릿에 계산 결과를 채워 문장 작성, 수치 목록·출처·KOSIS 링크 첨부 |

- 지표는 `config/catalog_notes.toml`의 지표 사전(40개)에 있습니다. 항목을 추가하면 지표가 늘어납니다.
- 답변 불가일 때는 `status`에 사유(`unknown_indicator`, `missing_region`, `out_of_range` 등)를 담아 다시 질문하는 방법을 안내합니다. 전체 목록은 `app/services/qa.py`의 `REFUSAL_GUIDE`에 있습니다.
- 요청 기록은 `data/requests/`에 쌓입니다. `python -m scripts.request_log`로 보고, `python -m scripts.missing_requests`로 DB에 없어 답하지 못한 요청을 집계합니다.

### 3. 평가와 결과

```bash
python -m scripts.eval_golden --golden eval/golden_sheet.csv --round 1 --interval 15   # 시트 60문항 자동 채점 (HCX 호출)
python -m scripts.eval_oracle data/eval/<채점 결과>.csv   # 답변 속 숫자를 pandas 정답과 대조 (HCX 없음)
python -m scripts.eval_replay                             # 기록된 HCX 해석으로 회귀 확인 (HCX 없음)
python -m scripts.check_raw                               # 원본 CSV ↔ 서비스 값 전수 대조
```

| 평가 | 결과 |
| --- | --- |
| 시트 60문항 자동 채점 (기대 행동) | 0회차 19/60 → 8회차 60/60 |
| pandas 정답 재계산 | 8회차 60/60, 환각 0/11 |
| 개발셋 16문항 (질의 이해·데이터 정확성) | 8회차 16/16, 11/11 |
| 원본 CSV ↔ 서비스 값 전수 대조 | 불일치 0 (계산 지표별 15,315개) |

- 기록: `eval/results/`(`golden_r0`, `golden_r7`, `oracle_r7`), 8회차는 `data/eval/golden_r8_20261028_030839.md`
- 같은 60문항을 보며 고친 결과라, 처음 보는 질문에 대한 성능이 아닙니다.
- 알려진 한계: 지역을 나열한 질문("경기도, 서울특별시, 인천광역시의 출생아 수")은 첫 지역 값만 답합니다. 합산은 "수도권" 같은 묶음 표현에서만 동작합니다.

## 프로젝트 구조

```
kosis-ai-service/
├── frontend/                # 채팅 화면 (FastAPI가 /chat으로 서빙)
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI 앱 진입점
│   │   ├── core/            # 환경변수 설정, DB 연결
│   │   ├── api/routes/      # API 엔드포인트 (health, ask, search)
│   │   ├── schemas/         # 요청·응답 모델
│   │   ├── prompts/         # LLM 프롬프트, 답변 정책
│   │   └── services/        # 비즈니스 로직 (검색: search·slots·synonyms·llm_pick·dialog / 수치: qa·indicators·lookup)
│   ├── pipeline/            # 전처리 DB·검색 데이터를 만드는 배치 작업
│   ├── config/              # 지표 사전(catalog_notes.toml), 일상어 사전(synonyms.json)
│   ├── scripts/             # 수집, 평가, 로그 보기, 점검 도구
│   ├── eval/                # 골든셋, 되묻기 시나리오, 발표용 결과(results/)
│   ├── data/                # 수집·생성 데이터 (각자 생성, 커밋 X)
│   ├── tests/               # pytest 테스트
│   └── docker/postgres/     # DB 최초 생성 시 확장 설치
├── docs/                    # 데이터 사전, 작업 기록
├── .devcontainer/           # VS Code 컨테이너 개발 환경 설정
├── compose.yaml             # 로컬 개발용 실행 설정 (api + db)
└── .env                     # 환경변수 (각자 생성, 커밋 X)
```
