import json
import re

from app.core.stats_db import rows as _rows


def _norm(text):
    return re.sub(r"\s+", "", str(text))


def find_indicator(text):
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
    ids = [t for t in dict.fromkeys(tbl_ids) if t]
    if not ids:
        return {}
    found = _rows("select tbl_id, survey_name from table_meta where tbl_id = any(?)", [ids])
    return {r["tbl_id"]: r["survey_name"] for r in found}


def _groups(tbl_ids):
    found = _rows(
        """
        select n.tbl_id, n.group_id, g.name from table_notes n join table_groups g using (group_id)
        where n.tbl_id = any(?)
    """,
        [list(tbl_ids)],
    )
    return {r["tbl_id"]: (r["group_id"], r["name"]) for r in found}


def _compatible(group_a, group_b):
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
    if tbl_a == tbl_b:
        return {"comparable": True, "reason": "같은 통계표입니다", "surveys": []}
    names = surveys([tbl_a, tbl_b])
    sa, sb = names.get(tbl_a), names.get(tbl_b)
    if sa is None or sb is None:
        return {"comparable": None, "reason": "조사를 알 수 없는 통계표입니다", "surveys": [sa, sb]}
    groups = _groups([tbl_a, tbl_b])
    if tbl_a in groups and tbl_b in groups:
        (ga, sa), (gb, sb) = groups[tbl_a], groups[tbl_b]
        same = ga == gb or _compatible(ga, gb)
    else:
        same = sa == sb
    if same:
        return {"comparable": True, "reason": f"같은 기준의 통계({sa})입니다", "surveys": [sa, sb]}
    reason = "두 조사는 기준 시점과 집계 대상이 달라 값을 직접 비교하지 않습니다"
    if any(s.startswith("장래") for s in (sa, sb)):
        reason += ". 장래 추계는 실제 통계가 아닌 추계값입니다"
    return {"comparable": False, "reason": reason, "surveys": [sa, sb]}
