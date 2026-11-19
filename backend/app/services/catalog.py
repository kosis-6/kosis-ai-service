"""통계표 카탈로그 조회 (하위 태스크 2: 답변 가능 판단, 3: 표 선택)

카탈로그 테이블은 pipeline/catalog.py가 config/catalog_notes.toml로 만든다.

  find_indicator(이름)     "고령화율" → 고령인구 비율, 어느 표에서 어떻게 구하는지
  table_info(표 ID)        조사 기준 설명
  comparable(표 A, 표 B)   두 표의 값을 직접 비교해도 되는지 (답변 불가 사례 3)
                           (cross_indicator.basis_gap이 차이 · 비교 답에서 쓴다)
"""

import json
import re

from app.core.stats_db import rows as _rows


def _norm(text):
    return re.sub(r"\s+", "", str(text))


def find_indicator(text):
    """지표 이름·별칭 → 지표 정보와 출처 표 목록"""
    key = _norm(text)
    for ind in _rows("select * from indicator_dict"):
        names = [ind["name"], *json.loads(ind["aliases"])]
        if key in {_norm(n) for n in names}:
            srcs = _rows(
                """
                select s.tbl_id, c.tbl_nm, s.item_ids, s.level, s.filters, s.start_year, s.end_year
                from indicator_sources s join catalog c using (tbl_id)
                where s.indicator = ? order by s.level desc nulls last
            """,
                [ind["name"]],
            )
            for s in srcs:
                s["filters"] = json.loads(s["filters"])
            return {
                "status": "ok",
                "name": ind["name"],
                "unit": ind["unit"],
                "method": ind["method"],
                "note": ind["note"],
                "sources": srcs,
                "projection": (
                    {"table": ind["projection_table"], "c3_id": ind["projection_c3"]}
                    if ind["projection_table"]
                    else None
                ),
            }
    return {
        "status": "unknown_indicator",
        "indicator": text,
        "supported": [r["name"] for r in _rows("select name from indicator_dict")],
    }


def table_info(tbl_id):
    """표의 조사 기준 설명"""
    rows = _rows(
        """
        select c.tbl_id, c.tbl_nm, c.start_year, c.end_year, c.age_unit, c.is_projection_table,
               n.survey, n.producer, n.reference_date, n.scope, n.note, n.verified, n.check_point,
               n.source_coverage, n.latest_available,
               g.group_id, g.name as group_name, g.measure
        from catalog c
        left join table_notes n using (tbl_id)
        left join table_groups g using (group_id)
        where c.tbl_id = ?
    """,
        [tbl_id],
    )
    return rows[0] if rows else None


def surveys(tbl_ids):
    """표 ID → 조사 이름 (table_meta.survey_name: 인구총조사, 주민등록인구현황 …).
    카탈로그에 없는 표(국토교통부 면적 표 등)는 빠진다"""
    ids = [t for t in dict.fromkeys(tbl_ids) if t]
    if not ids:
        return {}
    found = _rows("select tbl_id, survey_name from table_meta where tbl_id = any(?)", [ids])
    return {r["tbl_id"]: r["survey_name"] for r in found}


def _groups(tbl_ids):
    """표 설명서(config/catalog_notes.toml)에 적은 비교 그룹 {표 ID: (그룹 ID, 그룹 이름)}.
    그룹을 적지 않은 표는 빠진다"""
    found = _rows(
        """
        select n.tbl_id, n.group_id, g.name from table_notes n join table_groups g using (group_id)
        where n.tbl_id = any(?)
    """,
        [list(tbl_ids)],
    )
    return {r["tbl_id"]: (r["group_id"], r["name"]) for r in found}


def _compatible(group_a, group_b):
    """그룹이 달라도 직접 비교해도 되는 조합인가 (compatible_groups)"""
    return bool(
        _rows(
            """
            select 1 from compatible_groups
            where (group_a = ? and group_b = ?) or (group_a = ? and group_b = ?)
        """,
            [group_a, group_b, group_b, group_a],
        )
    )


def comparable(tbl_a, tbl_b):
    """두 표의 값을 직접 비교(빼기 · 크기 견주기)해도 되는지 (답변 불가 사례 3: 단위·조사 기준 차이)

    1. 같은 표면 된다
    2. 두 표 모두 표 설명서에 비교 그룹이 있으면 그룹으로 본다: 같은 그룹이거나
       compatible_groups에 적은 조합이면 된다 (출생 통계와 다문화 출생 통계)
    3. 아니면 조사 이름이 같아야 한다. 비교 그룹은 표 20개에만 적어 두었고, 나머지
       (인구총조사 표 1,800여 개 등)는 조사 이름으로 본다. 해마다 따로 있는 인구총조사 표끼리는 된다

    결과 {"comparable": True | False | None, "reason": 문장, "surveys": [A의 조사, B의 조사]}.
    None은 판단할 수 없음 (카탈로그에 없는 표): 부르는 쪽은 막지 않는다
    """
    if tbl_a == tbl_b:
        return {"comparable": True, "reason": "같은 통계표입니다", "surveys": []}
    names = surveys([tbl_a, tbl_b])
    sa, sb = names.get(tbl_a), names.get(tbl_b)
    if sa is None or sb is None:
        return {"comparable": None, "reason": "조사를 알 수 없는 통계표입니다", "surveys": [sa, sb]}
    groups = _groups([tbl_a, tbl_b])
    if tbl_a in groups and tbl_b in groups:
        (ga, sa), (gb, sb) = groups[tbl_a], groups[tbl_b]  # 그룹 이름은 그룹마다 다르다
        same = ga == gb or _compatible(ga, gb)
    else:
        same = sa == sb
    if same:
        return {"comparable": True, "reason": f"같은 기준의 통계({sa})입니다", "surveys": [sa, sb]}
    reason = "두 조사는 기준 시점과 집계 대상이 달라 값을 직접 비교하지 않습니다"
    if any(s.startswith("장래") for s in (sa, sb)):
        reason += ". 장래 추계는 실제 통계가 아닌 추계값입니다"
    return {"comparable": False, "reason": reason, "surveys": [sa, sb]}
