"""통계표 패밀리 묶기: stat_tables → table_families, family_axes

실전 2 기능 정의서 F-19 ③④ (검색 규칙 1장). 표를 합치지 않고 패밀리 → 멤버 → 판 관계만 기록한다.
검색은 표 단위로 하고, 결과를 패밀리로 접은 뒤 멤버·판을 고를 때(F-18) 이 테이블을 쓴다.

묶는 규칙
  판      같은 조사 + 이름 속 연도·신구 표기를 지운 표명이 같은 표.
          '시도별 외국인주민 현황(2007~2015)' / '(2006)' / 연도 없는 표 → 판 3개.
          수록 기간으로 구분하고, edition_order 1이 기본 판이다
          (가장 최신 연도를 담은 판 → 그중 기간이 긴 판, 규칙 2.4)
  멤버    표명 끝의 (계)/(남)/(여)로 성별 멤버, 그 앞의 '-대상'으로 대상 멤버를 나눈다.
          '한국국적을 취득한 자-혼인 귀화자(남)' → 패밀리 '한국국적을 취득한 자',
          대상 '혼인 귀화자', 성별 '남'. '-대상'이 없으면 대상 '전체'.
          이름에 표기가 없는 멤버 후보(같은 폴더·같은 구조, 지역으로 쪼갠 표)는
          사람이 확인한 뒤 넣는다 (규칙 1.4, 아직 반영 안 함)
  '-시도'·'-시군구'처럼 지역 수준만 다른 표는 다른 패밀리로 둔다 (stat_tables.region_levels로 판정)

출력
  table_families  표마다 family_id, family_name, target, sex, edition_order, edition
  family_axes     패밀리를 가르는 축(target / sex / edition)과 선택지. 선택지가 2개 이상인 축만
                  (F-14가 질문에 없는 축만 골라 되묻는다)
  두 테이블 모두 stat_tables에서 다시 만들 수 있으므로 실행할 때마다 새로 만든다.

실행 (stat_tables 적재 후, 서버 컨테이너 안)
  docker compose exec api python -m pipeline.build_families            # 묶기 + 보고
  docker compose exec api python -m pipeline.build_families --dry-run  # DB 없이 카탈로그로 보고만
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from typing import Any

# 이름 속 판 표기: '(2007~2015)', '(2006)', '(2011년~)', '(~1992)', '(2023~)'
EDITION_YEAR = re.compile(r"\s*\(\s*~?\s*\d{4}\s*년?\s*(?:~\s*(?:\d{4}\s*년?)?)?\s*\)")
# 분류 개정 판: '직업별(신) 사망자수(1993~2017)' / '직업별(구) 사망자수(~1992)'
EDITION_REVISION = re.compile(r"\((?:신|구)\)")
SEX_MEMBER = re.compile(r"\((계|남|여)\)")
TOTAL_TARGET = "전체"

DDL = """
DROP TABLE IF EXISTS table_families CASCADE;
CREATE TABLE table_families (
    tbl_id         text PRIMARY KEY REFERENCES stat_tables (tbl_id) ON DELETE CASCADE,
    family_id      text NOT NULL,
    family_name    text NOT NULL,
    target         text,
    sex            text,
    edition_order  int NOT NULL,
    edition        text NOT NULL
);
CREATE INDEX table_families_family_idx ON table_families (family_id);
CREATE VIEW family_axes AS
WITH options AS (
    SELECT family_id, 'target' AS axis, target AS option, min(edition_order) AS sort_key
    FROM table_families WHERE target IS NOT NULL GROUP BY family_id, target
    UNION ALL
    SELECT family_id, 'sex', sex, min(edition_order)
    FROM table_families WHERE sex IS NOT NULL GROUP BY family_id, sex
    UNION ALL
    SELECT family_id, 'edition', edition, min(edition_order)
    FROM table_families GROUP BY family_id, edition
)
SELECT family_id, axis, array_agg(option ORDER BY option <> '전체', sort_key, option) AS options
FROM options
GROUP BY family_id, axis
HAVING count(*) > 1;
"""


def edition_name(table_name: str) -> str:
    """판 표기를 지운 표명 ('…(계)(2007~2015)' → '…(계)')"""
    name = EDITION_REVISION.sub("", EDITION_YEAR.sub("", table_name))
    return re.sub(r"\s+", " ", name).strip()


def member_of(name: str) -> tuple[str, str | None, str | None]:
    """(family_name, target, sex). 이름에 성별 표기가 없으면 멤버가 하나뿐인 패밀리"""
    sex = SEX_MEMBER.search(name)
    if not sex:
        return name, None, None
    base = SEX_MEMBER.sub("", name).strip()
    family, _, target = base.rpartition("-")
    if not family:
        return base, TOTAL_TARGET, sex.group(1)
    return family.strip(), target.strip(), sex.group(1)


def edition_label(start_year: int, end_year: int) -> str:
    return str(start_year) if start_year == end_year else f"{start_year}~{end_year}"


def build(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tables: tbl_id, survey_name, table_name, start_year, end_year를 가진 dict 목록"""
    members: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in tables:
        members[(t["survey_name"], edition_name(t["table_name"]))].append(t)

    rows = []
    for (survey, name), editions in members.items():
        family_name, target, sex = member_of(name)
        # 최신 연도를 담은 판 → 기간이 긴 판 → 시작 연도가 이른 판 (규칙 2.4)
        editions.sort(key=lambda t: (-t["end_year"], t["start_year"], t["tbl_id"]))
        for order, t in enumerate(editions, start=1):
            rows.append(
                {
                    "tbl_id": t["tbl_id"],
                    "survey_name": survey,
                    "family_name": family_name,
                    "target": target,
                    "sex": sex,
                    "edition_order": order,
                    "edition": edition_label(t["start_year"], t["end_year"]),
                }
            )

    # family_id는 조사·패밀리명 순서로 매긴다 (카탈로그가 같으면 같은 번호)
    keys = sorted({(r["survey_name"], r["family_name"]) for r in rows})
    ids = {k: f"FAM_{i:04d}" for i, k in enumerate(keys, start=1)}
    for r in rows:
        r["family_id"] = ids[(r["survey_name"], r["family_name"])]
    return sorted(
        rows, key=lambda r: (r["family_id"], r["target"] or "", r["sex"] or "", r["edition_order"])
    )


def report(rows: list[dict[str, Any]]) -> str:
    size = Counter(r["family_id"] for r in rows)
    multi = {f for f, n in size.items() if n > 1}
    member_families = {r["family_id"] for r in rows if r["sex"]}
    edition_groups = Counter((r["family_id"], r["target"], r["sex"]) for r in rows)
    names = {r["family_id"]: r["family_name"] for r in rows}
    lines = [
        f"표 {len(rows)}개 → 패밀리 {len(size)}개 (표 2개 이상 {len(multi)}개,"
        f" {sum(size[f] for f in multi)}개 표)",
        f"판이 2개 이상인 멤버: {sum(n > 1 for n in edition_groups.values())}개"
        f" ({sum(n for n in edition_groups.values() if n > 1)}개 표)",
        f"대상·성별 멤버로 나뉜 패밀리: {len(member_families)}개"
        f" ({sum(size[f] for f in member_families)}개 표)",
        "표가 많은 패밀리: " + ", ".join(f"{names[f]} {n}" for f, n in size.most_common(5)),
    ]
    return "\n".join(lines)


def load(rows: list[dict[str, Any]], url: str) -> int:
    import psycopg

    cols = ["tbl_id", "family_id", "family_name", "target", "sex", "edition_order", "edition"]
    with psycopg.connect(url) as conn:  # 블록이 끝나면 커밋, 오류면 전부 되돌림
        conn.execute(DDL)
        with conn.cursor().copy(f"COPY table_families ({', '.join(cols)}) FROM STDIN") as copy:
            for r in rows:
                copy.write_row(tuple(r[c] for c in cols))
        return conn.execute("SELECT count(*) FROM family_axes").fetchone()[0]


def read_tables(url: str) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT tbl_id, survey_name, table_name, start_year, end_year FROM stat_tables"
        ).fetchall()


def main() -> int:
    from pipeline.load_catalog import database_url, read_catalog

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="DB 없이 카탈로그로 묶기·보고만")
    args = parser.parse_args()

    if args.dry_run:
        rows = build(read_catalog())
        print(report(rows))
        print("\n--dry-run: DB에는 넣지 않음")
        return 0
    url = database_url()
    tables = read_tables(url)
    if not tables:
        print("stat_tables가 비어 있음. pipeline.load_catalog를 먼저 실행", file=sys.stderr)
        return 1
    rows = build(tables)
    print(report(rows))
    axes = load(rows, url)
    print(f"\ntable_families 적재 완료: {len(rows)}행 · family_axes {axes}행")
    return 0


if __name__ == "__main__":
    sys.exit(main())
