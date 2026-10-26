"""카탈로그 보강: config/catalog_notes.toml → kosis.duckdb

하위 태스크 2(답변 가능 판단)·3(표 선택)에서 쓰는 설명 정보를 DB에 넣는다.

만들어지는 테이블
  table_groups       비교 그룹 (주민등록 / 인구총조사 / 장래추계 / 인구동향 ...)
  compatible_groups  그룹이 달라도 직접 비교해도 되는 조합
  table_notes        표마다 조사 이름, 기준 시점, 대상, 그룹, 원표 최신 시점, 확인 여부
  series_breaks      통계 기준이 바뀐 해 (예: 2010년 거주불명자 포함 시작)
  region_notes       특정 지역에만 해당하는 주의사항 (예: 장래추계 대구는 군위군 미포함)
  indicator_dict     지표 사전 (이름, 단위, 계산/조회 방식, 별칭)
  indicator_sources  지표 → 표·항목·지역 수준·수록 기간

조회 함수(find_indicator, table_info, comparable)는 서비스 코드인 app/services/catalog.py에 있다.

실행 순서: preprocess.py → catalog.py → validate.py
  (preprocess.py가 DB를 새로 만들기 때문에 catalog.py를 다시 실행해야 함)
실행: python pipeline/catalog.py   (가상환경 활성화 후, README 참고. 보통은 run_all.py로 실행)
"""

import json
import os
import re
import sys
import tomllib
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / os.environ.get("DUCKDB_PATH", "data/processed/kosis.duckdb")
NOTES_PATH = ROOT / "config" / "catalog_notes.toml"


def _norm(text):
    return re.sub(r"\s+", "", str(text))


def _as_list(v):
    return v if isinstance(v, list) else [v]


def load_notes():
    with open(NOTES_PATH, "rb") as f:
        return tomllib.load(f)


# ---- 1. DB에 넣기 ---------------------------------------------------------------


def build(con, notes):
    groups = [{"group_id": gid, **g} for gid, g in notes["groups"].items()]
    compat = [
        {"group_a": c["groups"][0], "group_b": c["groups"][1], "reason": c["reason"]}
        for c in notes.get("compatible_groups", [])
    ]
    tables = [
        {
            "tbl_id": tid,
            "survey": t.get("survey"),
            "producer": t.get("producer"),
            "group_id": t.get("group"),
            "reference_date": t.get("reference_date"),
            "scope": t.get("scope"),
            "note": t.get("note"),
            "source_coverage": t.get("source_coverage"),
            "latest_available": t.get("latest_available"),
            "verified": t.get("verified", False),
            "check_point": t.get("check"),
        }
        for tid, t in notes["tables"].items()
    ]
    breaks = [
        {"tbl_id": tid, "year": b["year"], "description": b["description"]}
        for tid, t in notes["tables"].items()
        for b in t.get("series_breaks", [])
    ]
    region_notes = [
        {
            "tbl_id": tid,
            "region_code": r["region"],
            "year": r.get("year"),
            "description": r["description"],
        }
        for tid, t in notes["tables"].items()
        for r in t.get("region_notes", [])
    ]
    indicators, sources = [], []
    for ind in notes["indicators"]:
        proj = ind.get("projection") or {}
        indicators.append(
            {
                "name": ind["name"],
                "unit": ind["unit"],
                "method": ind["method"],
                "aliases": json.dumps(ind.get("aliases", []), ensure_ascii=False),
                "note": ind.get("note"),
                "projection_table": proj.get("table"),
                "projection_c3": proj.get("c3_id"),
            }
        )
        for s in ind["sources"]:
            sources.append(
                {
                    "indicator": ind["name"],
                    "tbl_id": s["table"],
                    "item_ids": ",".join(_as_list(s["item_id"])) if "item_id" in s else None,
                    "level": s["level"],
                    "filters": json.dumps(s.get("filters", {})),
                }
            )

    for name, rows in [
        ("table_groups", groups),
        ("compatible_groups", compat),
        ("table_notes", tables),
        ("series_breaks", breaks),
        ("region_notes", region_notes),
        ("indicator_dict", indicators),
        ("indicator_sources_raw", sources),
    ]:
        con.sql(f"drop table if exists {name}")
        df = pd.DataFrame(rows)
        con.register("tmp_df", df)
        con.sql(f"create table {name} as select * from tmp_df")
        con.unregister("tmp_df")

    # 지표 출처에 수록 기간 붙이기
    con.sql("drop table if exists indicator_sources")
    con.sql("""
        create table indicator_sources as
        select s.*, c.start_year, c.end_year
        from indicator_sources_raw s left join catalog c using (tbl_id)
    """)
    con.sql("drop table indicator_sources_raw")


# ---- 2. 설정 파일 검사 ----------------------------------------------------------


def check(con, notes):
    """설정 파일이 실제 데이터와 맞는지 확인. 문제 목록을 돌려준다."""
    issues = []
    data_tables = {r[0] for r in con.sql("select tbl_id from catalog").fetchall()}
    note_tables = set(notes["tables"])
    group_ids = set(notes["groups"])

    for t in sorted(data_tables - note_tables):
        issues.append(f"설명이 없는 표: {t}")
    for t in sorted(note_tables - data_tables):
        issues.append(f"데이터에 없는 표가 설명에 있음: {t}")
    region_codes = {r[0] for r in con.sql("select region_code from regions").fetchall()}
    for tid, t in notes["tables"].items():
        if t.get("group") not in group_ids:
            issues.append(f"{tid}: 알 수 없는 그룹 '{t.get('group')}'")
        for r in t.get("region_notes", []):
            if r["region"] not in region_codes:
                issues.append(f"{tid}: region_notes의 지역 코드 '{r['region']}'가 regions에 없음")
    for c in notes.get("compatible_groups", []):
        for g in c["groups"]:
            if g not in group_ids:
                issues.append(f"compatible_groups: 알 수 없는 그룹 '{g}'")

    seen = {}
    for ind in notes["indicators"]:
        for alias in [ind["name"], *ind.get("aliases", [])]:
            key = _norm(alias)
            if key in seen and seen[key] != ind["name"]:
                issues.append(
                    f"별칭 '{alias}'이(가) '{seen[key]}'와 '{ind['name']}' 두 지표에 있음"
                )
            seen[key] = ind["name"]
        for s in ind["sources"]:
            if s["table"] not in data_tables:
                issues.append(f"{ind['name']}: 데이터에 없는 표 {s['table']}")
                continue
            for item in _as_list(s.get("item_id", [])):
                where = [f"tbl_id = '{s['table']}'", f"item_id = '{item}'"]
                # 값이 목록이면 그중 하나라도 있으면 됨 (lookup.py가 더해서 쓴다)
                where += [
                    f"{col} in ({', '.join(repr(v) for v in _as_list(val))})"
                    for col, val in s.get("filters", {}).items()
                ]
                n = con.sql(f"select count(*) from facts where {' and '.join(where)}").fetchall()[
                    0
                ][0]
                if n == 0:
                    issues.append(
                        f"{ind['name']}: {s['table']}에 항목 {item} "
                        f"{s.get('filters', '')} 자료가 없음"
                    )
        proj = ind.get("projection")
        if proj:
            n = con.sql(f"""select count(*) from facts
                            where tbl_id = '{proj["table"]}'
                              and c3_id = '{proj["c3_id"]}'""").fetchall()[0][0]
            if n == 0:
                issues.append(f"{ind['name']}: 추계 {proj} 자료가 없음")
    return issues


# ---- 실행 -----------------------------------------------------------------------


def main():
    if not DB_PATH.exists():
        raise SystemExit("먼저 pipeline/preprocess.py를 실행하세요.")
    notes = load_notes()
    con = duckdb.connect(str(DB_PATH))
    issues = check(con, notes)
    build(con, notes)
    n_tables, n_verified = con.sql(
        "select count(*), count(*) filter (where verified) from table_notes"
    ).fetchall()[0]
    n_ind, n_src = (
        con.sql("select count(*) from indicator_dict").fetchall()[0][0],
        con.sql("select count(*) from indicator_sources").fetchall()[0][0],
    )
    checks = con.sql(
        "select tbl_id, check_point from table_notes where check_point is not null order by 1"
    ).df()
    con.close()

    print(f"표 설명 {n_tables}개 (확인 완료 {n_verified}개), 지표 {n_ind}개, 지표 출처 {n_src}개")
    print("\n[설정 파일 검사]")
    print("\n".join(f"  - {i}" for i in issues) if issues else "  문제 없음")
    print("\n[확인이 필요한 항목 (check)]")
    print(checks.to_string(index=False))

    # 조회 함수 예시: 서비스 코드(app/)를 불러오려면 프로젝트 루트가 경로에 있어야 함
    sys.path.insert(0, str(ROOT))
    from app.services.catalog import comparable, find_indicator

    print("\n[예시] find_indicator('출산율')")
    print(json.dumps(find_indicator("출산율"), ensure_ascii=False, indent=2))
    for a, b in [
        ("DT_1B04006", "DT_1MR2060"),
        ("DT_1B04006", "DT_1BPB002"),
        ("DT_1B04006", "DT_1B8000H"),
        ("DT_1B8000H", "DT_1BB0006"),
    ]:
        print(f"\n[예시] comparable({a}, {b})")
        print(json.dumps(comparable(a, b), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
