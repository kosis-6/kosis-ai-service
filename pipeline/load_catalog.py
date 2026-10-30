"""통계표 카탈로그 적재: population_table_catalog_with_meta.jsonl → PostgreSQL stat_tables

실전 2 기능 정의서 F-01. 카탈로그를 정제하고 검색·조건 검증에 쓸 파생 필드를 붙여 저장한다.

입력: data/catalog/population_table_catalog_with_meta.jsonl
      (scripts/preprocess_population.py → scripts/enrich_catalog_meta.py 결과)
출력: PostgreSQL stat_tables
      테이블을 지우지 않고 tbl_id 기준으로 갱신한다 (새 표는 추가, 바뀐 표는 수정,
      카탈로그에서 빠진 표는 삭제). 임베딩·패밀리 같은 테이블이 tbl_id로 이 테이블을
      참조해도 다시 적재할 때 지워지지 않게 하기 위해서다.
      열 구성을 바꿨을 때만 --recreate로 테이블을 새로 만든다.

정제
  - 표명·분류명·항목명의 앞뒤 공백, 연속 공백, 앞에 붙은 따옴표 정리
  - 원본 필드(분류·항목·주석·경로)는 JSONB로 그대로 보관

파생 필드
  start_year, end_year   수록 기간의 연도 (월·분기 표는 YYYYMM 앞 4자리, 주기가 여럿이면 전체 범위)
  cycles                 주기 (5년, 년, 2년, 월, 부정기, 분기, IR)
  region_levels          현거주지 기준 지역 수준 (시도 / 시군구 / 읍면동)
  prev_region_levels     5년전·1년전 거주지 분류의 지역 수준 (이동 질문에서만 지역 축으로 씀)
  sex_source             성별이 어디 있나: class(성별 분류) / item(항목 이름)
                         / member(표 이름의 (계)(남)(여)) / none
  sex_coverage           both / male_only / female_only / total_only((계) 표) / none
  has_total              성별 합계(계·합계·전체 등)가 있는가. 성별이 없는 표와 (남)/(여) 표는 NULL
                         ((남)/(여) 표의 합계는 패밀리 단위로 판정, 검색 규칙 7장)
  is_single_period, has_comment, url

실행 (서버 컨테이너 안, DB가 떠 있어야 함)
  docker compose exec api python -m pipeline.load_catalog            # 적재 + 보고
  docker compose exec api python -m pipeline.load_catalog --dry-run  # DB 없이 정제·보고만
  docker compose exec api python -m pipeline.load_catalog --recreate # 테이블을 새로 만든 뒤 적재
"""

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "data" / "catalog" / "population_table_catalog_with_meta.jsonl"
EXPECTED_TABLES = 2364  # 국가데이터처 제공 표 수
KOSIS_URL = "https://kosis.kr/statHtml/statHtml.do?orgId={org_id}&tblId={tbl_id}"

# 합계 이름 (검색 규칙 7장). 성별 분류 값과 항목 이름에서 합계를 찾을 때 쓴다
TOTAL_NAMES = {"계", "합계", "전체", "총계", "인구", "총인구", "소계"}
MALE = {"남", "남자", "남성", "남편"}
FEMALE = {"여", "여자", "여성", "아내"}
# 성별 분류: '성별', '내외국인 및 성별', '성 및 농가비농가별' 등. 가구주 성별은 다른 축이라 뺀다
SEX_CLASS_EXCLUDE = re.compile(r"가구주|성씨|세대|구성|특성|정체성|소속감")
# 항목 이름 속 성별 단어: '남자', '총인구_남자', '남자-미혼', '남-현거주지', '고령자 남자'
SEX_TOKEN = re.compile(r"(?:^|[_\s(·,-])(남|남자|남성|여|여자|여성)(?=$|[_\s)·,-])")
# 표 자체가 성별로 나뉜 경우: '한국국적을 취득한 자-혼인 귀화자(남)', '…(여)(2007~2015)'
SEX_MEMBER = re.compile(r"\((계|남|여)\)")
# 이전 거주지 분류: '5년전 거주지별', '1년전거주지별'
PREV_REGION = re.compile(r"\d+\s*년\s*전")
REGION_LEVEL_ORDER = ["시도", "시군구", "읍면동"]

COLUMNS = [
    "tbl_id",
    "org_id",
    "organization",
    "survey_name",
    "table_name",
    "topic",
    "source_text",
    "period",
    "catalog_periods",
    "paths",
    "path_texts",
    "classifications",
    "items",
    "comments",
    "comment_text",
    "meta_summary",
    "start_year",
    "end_year",
    "cycles",
    "is_single_period",
    "region_levels",
    "prev_region_levels",
    "sex_source",
    "sex_coverage",
    "has_total",
    "has_comment",
    "url",
]
JSON_COLUMNS = {"catalog_periods", "paths", "classifications", "items", "comments"}

DROP = "DROP TABLE IF EXISTS stat_tables CASCADE"
DDL = """
CREATE TABLE IF NOT EXISTS stat_tables (
    tbl_id              text PRIMARY KEY,
    org_id              text NOT NULL,
    organization        text NOT NULL,
    survey_name         text NOT NULL,
    table_name          text NOT NULL,
    topic               text,
    source_text         text,
    period              text,
    catalog_periods     jsonb NOT NULL,
    paths               jsonb NOT NULL,
    path_texts          text[] NOT NULL,
    classifications     jsonb NOT NULL,
    items               jsonb NOT NULL,
    comments            jsonb NOT NULL,
    comment_text        text,
    meta_summary        text,
    start_year          int NOT NULL,
    end_year            int NOT NULL,
    cycles              text[] NOT NULL,
    is_single_period    boolean NOT NULL,
    region_levels       text[] NOT NULL,
    prev_region_levels  text[] NOT NULL,
    sex_source          text NOT NULL,
    sex_coverage        text NOT NULL,
    has_total           boolean,
    has_comment         boolean NOT NULL,
    url                 text NOT NULL,
    loaded_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS stat_tables_survey_idx ON stat_tables (survey_name);
CREATE INDEX IF NOT EXISTS stat_tables_years_idx ON stat_tables (start_year, end_year);
"""


def clean_text(text: str | None) -> str | None:
    """앞뒤 공백·연속 공백·앞에 붙은 따옴표를 정리한다 ("'세대구성별" → "세대구성별")"""
    if text is None:
        return None
    return re.sub(r"\s+", " ", text).strip().lstrip("'\"").strip() or None


# ---- 1. 파생 필드 ---------------------------------------------------------------


def years(periods: list[dict[str, str]]) -> tuple[int, int]:
    return min(int(p["start"][:4]) for p in periods), max(int(p["end"][:4]) for p in periods)


def region_levels(classifications: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """지역 분류의 level_names를 현거주지 / 이전 거주지로 나눠 모은다"""
    current: set[str] = set()
    previous: set[str] = set()
    for c in classifications:
        levels = c.get("level_names") or []
        (previous if PREV_REGION.search(c["name"]) else current).update(levels)

    def ordered(levels: set[str]) -> list[str]:
        return [lv for lv in REGION_LEVEL_ORDER if lv in levels]

    return ordered(current), ordered(previous)


def is_sex_class(name: str) -> bool:
    return "성" in name and not SEX_CLASS_EXCLUDE.search(name)


def coverage(male: bool, female: bool) -> str:
    if male and female:
        return "both"
    if male:
        return "male_only"
    if female:
        return "female_only"
    return "none"


def sex_info(
    table_name: str, classifications: list[dict[str, Any]], items: list[dict[str, Any]]
) -> tuple[str, str, bool | None]:
    """(sex_source, sex_coverage, has_total). 분류 → 항목 → 표 이름 순으로 본다"""
    for c in classifications:
        if is_sex_class(c["name"]):
            values = {v.strip() for v in c.get("sample_values") or []}
            sex_coverage = coverage(bool(values & MALE), bool(values & FEMALE))
            # '성연령배우관계학력별'처럼 이름에 '성'이 있어도 값에 남/여가 없으면 성별 분류가 아니다
            if sex_coverage != "none":
                return "class", sex_coverage, bool(values & TOTAL_NAMES)

    names = [i["name"] for i in items]
    tokens = {m.group(1) for n in names for m in SEX_TOKEN.finditer(n)}
    if tokens:
        sex_coverage = coverage(bool(tokens & MALE), bool(tokens & FEMALE))
        # 기혼여성처럼 한 성별만 다루는 표는 남녀 합계를 따질 필요가 없다
        has_total = _item_total(names) if sex_coverage == "both" else None
        return "item", sex_coverage, has_total

    member = SEX_MEMBER.search(table_name)
    if member:
        # (남)/(여) 표의 합계는 같은 패밀리의 (계) 표에 있으므로 표 단위로는 판정하지 않는다
        label = member.group(1)
        if label == "계":
            return "member", "total_only", True
        return "member", "male_only" if label == "남" else "female_only", None

    return "none", "none", None


def _item_total(names: list[str]) -> bool:
    """항목으로 남/여가 나뉜 표에 합계 항목이 있는가.

    - 첫 항목이 성별 항목이 아니면 합계로 본다 ('외국인', '남자', '여자' / '취업인구-계', '남자', …)
      (검색 규칙 10장 미결: "남/여가 아닌 첫 항목 = 합계" 추정, 표본 확인 필요)
    - '총인구'·'계'처럼 합계 이름이 있거나, '총인구_남자'의 앞부분 '총인구'처럼
      성별 항목의 바탕이 되는 항목이 따로 있어도 합계가 있다고 본다.
    """
    if names and not SEX_TOKEN.search(names[0]):
        return True
    bases = set()
    for n in names:
        m = SEX_TOKEN.search(n)
        if m:
            bases.add(n[: m.start()].strip(" _-"))
    for n in names:
        if SEX_TOKEN.search(n):
            continue
        base = re.sub(r"[_-]?(계|합계|전체)$", "", n).strip(" _-")
        if n in TOTAL_NAMES or base in TOTAL_NAMES or (base and base in bases):
            return True
    return False


# ---- 2. 정제 --------------------------------------------------------------------


def clean_record(raw: dict[str, Any]) -> dict[str, Any]:
    classifications = [
        {
            **c,
            "name": clean_text(c["name"]),
            "sample_values": [clean_text(v) for v in c.get("sample_values") or []],
        }
        for c in raw["classifications"]
    ]
    items = [{**i, "name": clean_text(i["name"])} for i in raw["items"]]
    table_name = clean_text(raw["table_name"])
    start_year, end_year = years(raw["catalog_periods"])
    current, previous = region_levels(classifications)
    sex_source, sex_coverage, has_total = sex_info(table_name, classifications, items)
    comment_text = (raw.get("comment_text") or "").strip() or None
    return {
        "tbl_id": raw["tbl_id"].strip(),
        "org_id": raw["org_id"].strip(),
        "organization": clean_text(raw["organization"]),
        "survey_name": clean_text(raw["survey_name"]),
        "table_name": table_name,
        "topic": clean_text(raw.get("topic")),
        "source_text": clean_text(raw.get("source_text")),
        "period": clean_text(raw.get("period")),
        "catalog_periods": raw["catalog_periods"],
        "paths": raw["paths"],
        "path_texts": [clean_text(p["path"]) for p in raw["paths"]],
        "classifications": classifications,
        "items": items,
        "comments": raw.get("comments") or [],
        "comment_text": comment_text,
        "meta_summary": raw.get("meta_summary"),
        "start_year": start_year,
        "end_year": end_year,
        "cycles": list(dict.fromkeys(p["frequency_label"] for p in raw["catalog_periods"])),
        "is_single_period": start_year == end_year,
        "region_levels": current,
        "prev_region_levels": previous,
        "sex_source": sex_source,
        "sex_coverage": sex_coverage,
        "has_total": has_total,
        "has_comment": comment_text is not None,
        "url": KOSIS_URL.format(org_id=raw["org_id"].strip(), tbl_id=raw["tbl_id"].strip()),
    }


def read_catalog(path: Path = CATALOG) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [clean_record(json.loads(line)) for line in f if line.strip()]


def check(records: list[dict[str, Any]]) -> list[str]:
    """완료 기준 (F-01): 표 수 = 제공 표 수, tbl_id 중복 0, 분류·항목 결측 0"""
    errors = []
    if len(records) != EXPECTED_TABLES:
        errors.append(f"표 수 {len(records)} ≠ 제공 표 수 {EXPECTED_TABLES}")
    dup = [k for k, n in Counter(r["tbl_id"] for r in records).items() if n > 1]
    if dup:
        errors.append(f"tbl_id 중복 {len(dup)}개: {dup[:5]}")
    for field in ("classifications", "items"):
        empty = [r["tbl_id"] for r in records if not r[field]]
        if empty:
            errors.append(f"{field} 결측 {len(empty)}개: {empty[:5]}")
    return errors


def report(records: list[dict[str, Any]]) -> str:
    def dist(key: str) -> str:
        counts = Counter(r[key] for r in records)
        return ", ".join(f"{k} {v}" for k, v in counts.most_common())

    deepest = Counter(r["region_levels"][-1] if r["region_levels"] else "없음" for r in records)
    no_total = Counter(r["sex_source"] for r in records if r["has_total"] is False)
    lines = [
        f"표 {len(records)}개 · 조사 {len({r['survey_name'] for r in records})}개"
        f" · 기관 {len({r['organization'] for r in records})}개",
        f"주석 있음 {sum(r['has_comment'] for r in records)}"
        f" · 경로 2개 이상 {sum(len(r['path_texts']) > 1 for r in records)}"
        f" · 2024년 이후까지 수록 {sum(r['end_year'] >= 2024 for r in records)}",
        f"주기: {Counter(c for r in records for c in r['cycles']).most_common()}",
        f"지역 수준(가장 세밀한 단계): {deepest.most_common()}",
        f"이전 거주지 분류 있음: {sum(bool(r['prev_region_levels']) for r in records)}",
        f"성별 위치: {dist('sex_source')}",
        f"성별 제공 방식: {dist('sex_coverage')}",
        f"성별은 있는데 합계 없음: {sum(no_total.values())} ({dict(no_total)})",
    ]
    return "\n".join(lines)


# ---- 3. DB에 넣기 ---------------------------------------------------------------


def database_url() -> str:
    from app.core.config import get_settings

    # SQLAlchemy용 주소(postgresql+psycopg://)를 psycopg가 읽는 형식으로 바꾼다
    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def load(records: list[dict[str, Any]], url: str, recreate: bool = False) -> dict[str, int]:
    """임시 테이블에 COPY한 뒤 tbl_id 기준으로 stat_tables를 갱신한다"""
    import psycopg
    from psycopg.types.json import Jsonb

    rows = [tuple(Jsonb(r[c]) if c in JSON_COLUMNS else r[c] for c in COLUMNS) for r in records]
    cols = ", ".join(COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS if c != "tbl_id")
    with psycopg.connect(url) as conn:  # 블록이 끝나면 커밋, 오류면 전부 되돌림
        if recreate:
            conn.execute(DROP)
        conn.execute(DDL)
        conn.execute(
            "CREATE TEMP TABLE incoming (LIKE stat_tables INCLUDING DEFAULTS) ON COMMIT DROP"
        )
        with conn.cursor().copy(f"COPY incoming ({cols}) FROM STDIN") as copy:
            for row in rows:
                copy.write_row(row)
        # xmax = 0이면 새로 들어간 행, 아니면 기존 행을 고친 것
        upserted = conn.execute(
            f"""INSERT INTO stat_tables ({cols}) SELECT {cols} FROM incoming
                ON CONFLICT (tbl_id) DO UPDATE SET {updates}, loaded_at = now()
                RETURNING xmax = 0"""
        ).fetchall()
        deleted = conn.execute(
            "DELETE FROM stat_tables s WHERE NOT EXISTS "
            "(SELECT 1 FROM incoming i WHERE i.tbl_id = s.tbl_id)"
        ).rowcount
        total = conn.execute("SELECT count(*) FROM stat_tables").fetchone()[0]
    inserted = sum(1 for (is_new,) in upserted if is_new)
    return {
        "inserted": inserted,
        "updated": len(upserted) - inserted,
        "deleted": deleted,
        "total": total,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="DB에 넣지 않고 정제·보고만")
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="열 구성을 바꿨을 때 테이블을 새로 만든다 (참조하는 테이블의 외래 키도 함께 지워짐)",
    )
    args = parser.parse_args()

    records = read_catalog()
    print(report(records))
    errors = check(records)
    if errors:
        print("\n완료 기준 미달:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    if args.dry_run:
        print("\n--dry-run: DB에는 넣지 않음")
        return 0
    result = load(records, database_url(), recreate=args.recreate)
    print(
        f"\nstat_tables 적재 완료: {result['total']}행"
        f" (추가 {result['inserted']}, 갱신 {result['updated']}, 삭제 {result['deleted']})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
