"""통계표 카탈로그 조회 (하위 태스크 2: 답변 가능 판단, 3: 표 선택)

카탈로그 테이블은 pipeline/catalog.py가 config/catalog_notes.toml로 만든다.

  find_indicator(이름)     "고령화율" → 고령인구 비율, 어느 표에서 어떻게 구하는지
  table_info(표 ID)        조사 기준 설명
  comparable(표 A, 표 B)   두 표의 값을 직접 비교해도 되는지 (답변 불가 사례 3)
"""

import json
import re

from app.core.duckdb import rows as _rows


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
                where s.indicator = ? order by s.level desc
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


def comparable(tbl_a, tbl_b):
    """두 표의 값을 직접 비교해도 되는지 판단 (답변 불가 사례 3)"""
    a, b = table_info(tbl_a), table_info(tbl_b)
    if a is None or b is None:
        return {"comparable": False, "reason": "알 수 없는 통계표가 포함되어 있습니다."}
    if tbl_a == tbl_b or a["group_id"] == b["group_id"]:
        return {"comparable": True, "reason": f"같은 기준의 통계({a['group_name']})입니다."}
    compat = _rows(
        """
        select reason from compatible_groups
        where (group_a = ? and group_b = ?) or (group_a = ? and group_b = ?)
    """,
        [a["group_id"], b["group_id"], b["group_id"], a["group_id"]],
    )
    if compat:
        return {"comparable": True, "reason": compat[0]["reason"]}

    def desc(t):
        return f"{t['survey']}({t['reference_date']}, {t['scope']})"

    if a["measure"] != b["measure"]:
        reason = (
            f"{desc(a)}와 {desc(b)}는 종류가 다른 통계입니다 "
            "(특정 시점의 인구 수 vs 1년간 발생한 건수). 값을 직접 비교하지 않습니다."
        )
    else:
        reason = f"{desc(a)}와 {desc(b)}는 기준 시점·대상이 달라 값을 직접 비교하지 않습니다."
    if a["is_projection_table"] or b["is_projection_table"]:
        reason += " 장래인구추계는 실제 통계가 아닌 추계값입니다."
    return {
        "comparable": False,
        "reason": reason,
        "tables": [
            {
                "tbl_id": t["tbl_id"],
                "survey": t["survey"],
                "reference_date": t["reference_date"],
                "scope": t["scope"],
            }
            for t in (a, b)
        ],
    }
