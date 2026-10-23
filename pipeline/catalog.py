"""카탈로그 보강: config/catalog_notes.toml → kosis.duckdb

하위 태스크 2(답변 가능 판단)·3(표 선택)에서 쓰는 설명 정보를 DB에 넣고, 조회 함수를 제공한다.

만들어지는 테이블
  table_groups       비교 그룹 (주민등록 / 인구총조사 / 장래추계 / 인구동향 ...)
  compatible_groups  그룹이 달라도 직접 비교해도 되는 조합
  table_notes        표마다 조사 이름, 기준 시점, 대상, 그룹, 원표 최신 시점, 확인 여부
  series_breaks      통계 기준이 바뀐 해 (예: 2010년 거주불명자 포함 시작)
  region_notes       특정 지역에만 해당하는 주의사항 (예: 장래추계 대구는 군위군 미포함)
  indicator_dict     지표 사전 (이름, 단위, 계산/조회 방식, 별칭)
  indicator_sources  지표 → 표·항목·지역 수준·수록 기간

조회 함수
  find_indicator(이름)     "고령화율" → 고령인구 비율, 어느 표에서 어떻게 구하는지
  table_info(표 ID)        조사 기준 설명
  comparable(표 A, 표 B)   두 표의 값을 직접 비교해도 되는지 (답변 불가 사례 3)

실행 순서: preprocess.py → catalog.py → validate.py
  (preprocess.py가 DB를 새로 만들기 때문에 catalog.py를 다시 실행해야 함)
실행: python pipeline/catalog.py   (가상환경 활성화 후, README 참고. 보통은 run_all.py로 실행)
"""
import json
import re
import tomllib
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "processed" / "kosis.duckdb"
NOTES_PATH = ROOT / "config" / "catalog_notes.toml"

_CON = None


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
    compat = [{"group_a": c["groups"][0], "group_b": c["groups"][1], "reason": c["reason"]}
              for c in notes.get("compatible_groups", [])]
    tables = [{
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
    } for tid, t in notes["tables"].items()]
    breaks = [{"tbl_id": tid, "year": b["year"], "description": b["description"]}
              for tid, t in notes["tables"].items() for b in t.get("series_breaks", [])]
    region_notes = [{"tbl_id": tid, "region_code": r["region"], "year": r.get("year"), "description": r["description"]}
                    for tid, t in notes["tables"].items() for r in t.get("region_notes", [])]
    indicators, sources = [], []
    for ind in notes["indicators"]:
        proj = ind.get("projection") or {}
        indicators.append({
            "name": ind["name"],
            "unit": ind["unit"],
            "method": ind["method"],
            "aliases": json.dumps(ind.get("aliases", []), ensure_ascii=False),
            "note": ind.get("note"),
            "projection_table": proj.get("table"),
            "projection_c3": proj.get("c3_id"),
        })
        for s in ind["sources"]:
            sources.append({
                "indicator": ind["name"],
                "tbl_id": s["table"],
                "item_ids": ",".join(_as_list(s["item_id"])) if "item_id" in s else None,
                "level": s["level"],
                "filters": json.dumps(s.get("filters", {})),
            })

    for name, rows in [("table_groups", groups), ("compatible_groups", compat), ("table_notes", tables),
                       ("series_breaks", breaks), ("region_notes", region_notes),
                       ("indicator_dict", indicators), ("indicator_sources_raw", sources)]:
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
                issues.append(f"별칭 '{alias}'이(가) '{seen[key]}'와 '{ind['name']}' 두 지표에 있음")
            seen[key] = ind["name"]
        for s in ind["sources"]:
            if s["table"] not in data_tables:
                issues.append(f"{ind['name']}: 데이터에 없는 표 {s['table']}")
                continue
            for item in _as_list(s.get("item_id", [])):
                where = [f"tbl_id = '{s['table']}'", f"item_id = '{item}'"]
                where += [f"{col} = '{val}'" for col, val in s.get("filters", {}).items()]
                n = con.sql(f"select count(*) from facts where {' and '.join(where)}").fetchall()[0][0]
                if n == 0:
                    issues.append(f"{ind['name']}: {s['table']}에 항목 {item} {s.get('filters', '')} 자료가 없음")
        proj = ind.get("projection")
        if proj:
            n = con.sql(f"""select count(*) from facts
                            where tbl_id = '{proj['table']}' and c3_id = '{proj['c3_id']}'""").fetchall()[0][0]
            if n == 0:
                issues.append(f"{ind['name']}: 추계 {proj} 자료가 없음")
    return issues


# ---- 3. 조회 함수 ---------------------------------------------------------------

def _con():
    global _CON
    if _CON is None:
        _CON = duckdb.connect(str(DB_PATH), read_only=True)
    return _CON


def _rows(sql, params=()):
    cur = _con().execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def find_indicator(text):
    """지표 이름·별칭 → 지표 정보와 출처 표 목록"""
    key = _norm(text)
    for ind in _rows("select * from indicator_dict"):
        names = [ind["name"], *json.loads(ind["aliases"])]
        if key in {_norm(n) for n in names}:
            srcs = _rows("""
                select s.tbl_id, c.tbl_nm, s.item_ids, s.level, s.filters, s.start_year, s.end_year
                from indicator_sources s join catalog c using (tbl_id)
                where s.indicator = ? order by s.level desc
            """, [ind["name"]])
            for s in srcs:
                s["filters"] = json.loads(s["filters"])
            return {"status": "ok", "name": ind["name"], "unit": ind["unit"], "method": ind["method"],
                    "note": ind["note"], "sources": srcs,
                    "projection": ({"table": ind["projection_table"], "c3_id": ind["projection_c3"]}
                                   if ind["projection_table"] else None)}
    return {"status": "unknown_indicator", "indicator": text,
            "supported": [r["name"] for r in _rows("select name from indicator_dict")]}


def table_info(tbl_id):
    """표의 조사 기준 설명"""
    rows = _rows("""
        select c.tbl_id, c.tbl_nm, c.start_year, c.end_year, c.age_unit, c.is_projection_table,
               n.survey, n.producer, n.reference_date, n.scope, n.note, n.verified, n.check_point,
               n.source_coverage, n.latest_available,
               g.group_id, g.name as group_name, g.measure
        from catalog c
        left join table_notes n using (tbl_id)
        left join table_groups g using (group_id)
        where c.tbl_id = ?
    """, [tbl_id])
    return rows[0] if rows else None


def comparable(tbl_a, tbl_b):
    """두 표의 값을 직접 비교해도 되는지 판단 (답변 불가 사례 3)"""
    a, b = table_info(tbl_a), table_info(tbl_b)
    if a is None or b is None:
        return {"comparable": False, "reason": "알 수 없는 통계표가 포함되어 있습니다."}
    if tbl_a == tbl_b or a["group_id"] == b["group_id"]:
        return {"comparable": True, "reason": f"같은 기준의 통계({a['group_name']})입니다."}
    compat = _rows("""
        select reason from compatible_groups
        where (group_a = ? and group_b = ?) or (group_a = ? and group_b = ?)
    """, [a["group_id"], b["group_id"], b["group_id"], a["group_id"]])
    if compat:
        return {"comparable": True, "reason": compat[0]["reason"]}

    def desc(t):
        return f"{t['survey']}({t['reference_date']}, {t['scope']})"

    if a["measure"] != b["measure"]:
        reason = (f"{desc(a)}와 {desc(b)}는 종류가 다른 통계입니다 "
                  "(특정 시점의 인구 수 vs 1년간 발생한 건수). 값을 직접 비교하지 않습니다.")
    else:
        reason = f"{desc(a)}와 {desc(b)}는 기준 시점·대상이 달라 값을 직접 비교하지 않습니다."
    if a["is_projection_table"] or b["is_projection_table"]:
        reason += " 장래인구추계는 실제 통계가 아닌 추계값입니다."
    return {"comparable": False, "reason": reason,
            "tables": [{"tbl_id": t["tbl_id"], "survey": t["survey"], "reference_date": t["reference_date"],
                        "scope": t["scope"]} for t in (a, b)]}


# ---- 실행 -----------------------------------------------------------------------

def main():
    if not DB_PATH.exists():
        raise SystemExit("먼저 pipeline/preprocess.py를 실행하세요.")
    notes = load_notes()
    con = duckdb.connect(str(DB_PATH))
    issues = check(con, notes)
    build(con, notes)
    n_tables, n_verified = con.sql("select count(*), count(*) filter (where verified) from table_notes").fetchall()[0]
    n_ind, n_src = (con.sql("select count(*) from indicator_dict").fetchall()[0][0],
                    con.sql("select count(*) from indicator_sources").fetchall()[0][0])
    checks = con.sql("select tbl_id, check_point from table_notes where check_point is not null order by 1").df()
    con.close()

    print(f"표 설명 {n_tables}개 (확인 완료 {n_verified}개), 지표 {n_ind}개, 지표 출처 {n_src}개")
    print("\n[설정 파일 검사]")
    print("\n".join(f"  - {i}" for i in issues) if issues else "  문제 없음")
    print("\n[확인이 필요한 항목 (check)]")
    print(checks.to_string(index=False))

    print("\n[예시] find_indicator('출산율')")
    print(json.dumps(find_indicator("출산율"), ensure_ascii=False, indent=2))
    for a, b in [("DT_1B04006", "DT_1MR2060"), ("DT_1B04006", "DT_1BPB002"),
                 ("DT_1B04006", "DT_1B8000H"), ("DT_1B8000H", "DT_1BB0006")]:
        print(f"\n[예시] comparable({a}, {b})")
        print(json.dumps(comparable(a, b), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
