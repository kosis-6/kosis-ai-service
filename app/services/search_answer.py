"""검색 결과 → API 응답 (실전 2 F-07 안내 응답 생성, 기능 정의서 5장 형식)

표명·조사명·기관·기간·링크는 DB 값만 쓴다. LLM이 만든 표 이름이나 ID는 내보내지 않는다.
답변 문장은 status별 템플릿으로 만든다 (실전 1 qa.summary와 같은 방식).
"""

from typing import Any

from app.services import notices

CHECK_LABELS = {"year": "연도", "region_level": "지역 수준", "sex": "성별", "target": "대상"}
STATUS_MESSAGES = {
    "out_of_scope": (
        "제공 범위(KOSIS 인구 분야 통계표)에서 질문에 맞는 통계표를 찾지 못했습니다. "
        "찾는 통계의 주제나 표 이름에 들어갈 단어로 다시 질문해 주세요."
    ),
    "not_statistics": (
        "통계표를 찾아 드리는 서비스입니다. 이렇게 질문해 보세요.\n"
        "예) 2024년 시군구별 외국인 주민 수 / 연령별 혼인 건수 / 1인 가구 통계표"
    ),
    "parse_error": "질문을 이해하지 못했습니다. 찾는 통계의 주제를 넣어 다시 질문해 주세요.",
}
NUMBERS = "①②③④⑤"
AXIS_LABELS = {"target": "대상", "sex": "성별", "edition": "연도"}


def check_label(key: str, slots: dict[str, Any]) -> str:
    """'class:연령' → '연령', 'year' → '2010년'"""
    c = slots["conditions"]
    if key == "year":
        return f"{c['year']}~{c['year_end']}년" if c["year_end"] else f"{c['year']}년"
    if key == "region_level":
        return c["region_level"]
    return key.split(":", 1)[1] if ":" in key else CHECK_LABELS.get(key, key)


def member_label(t: dict[str, Any]) -> str:
    """'혼인 귀화자(여)', '전체(계)'. 멤버가 하나뿐인 패밀리는 표명"""
    if t["target"] is None:
        return t["table_name"]
    return f"{t['target']}({t['sex']})" if t["sex"] else t["target"]


def recommendation(
    rank: int,
    t: dict[str, Any],
    family: list[dict[str, Any]],
    checks: dict,
    score: float | None,
    slots: dict[str, Any],
) -> dict[str, Any]:
    same_member = [r for r in family if (r["target"], r["sex"]) == (t["target"], t["sex"])]
    members = {}
    for r in sorted(family, key=lambda r: r["edition_order"]):
        key = (r["target"], r["sex"])
        if key != (t["target"], t["sex"]):
            members.setdefault(
                key,
                {
                    "tbl_id": r["tbl_id"],
                    "label": member_label(r),
                    "target": r["target"],
                    "sex": r["sex"],
                },
            )
    return {
        "rank": rank,
        "tbl_id": t["tbl_id"],
        "org_id": t["org_id"],
        "tbl_nm": t["table_name"],
        "survey_nm": t["survey_name"],
        "org_nm": t["organization"],
        "period": t["edition"],
        "path": (t["path_texts"] or [None])[0],
        "url": t["url"],
        "checks": checks,
        "family": {
            "id": t["family_id"],
            "name": t["family_name"],
            "current": {"target": t["target"], "sex": t["sex"]},
            "members": list(members.values()),
            "editions": [
                {"tbl_id": r["tbl_id"], "period": r["edition"], "tbl_nm": r["table_name"]}
                for r in sorted(same_member, key=lambda r: -r["end_year"])
                if r["tbl_id"] != t["tbl_id"]
            ],
        },
        "notes": notices.notices(t, family, slots) + notes(t, family),
        "score": None if score is None else round(score, 3),
    }


def notes(t: dict[str, Any], family: list[dict[str, Any]]) -> list[str]:
    """주석 밖의 고지: 이름이 바뀐 판, 남녀 합계 없음 (주석 고지는 notices.py)"""
    out = []
    by_id = {r["tbl_id"]: r for r in family}
    member = [r for r in family if (r["target"], r["sex"]) == (t["target"], t["sex"])]
    if t.get("renamed_from") in by_id:
        old = by_id[t["renamed_from"]]
        out.append(f"{old['edition']}년 자료는 이름이 다른 ‘{old['table_name']}’ 표에 있습니다.")
    for r in member:  # 옛 판을 고른 경우: 이후 자료는 이름이 바뀐 표에 있다
        if r.get("renamed_from") == t["tbl_id"]:
            out.append(f"{r['edition']}년 자료는 이름이 바뀐 ‘{r['table_name']}’ 표에 있습니다.")
    if t["has_total"] is False:
        out.append(
            "이 표에는 남녀 합계가 없습니다. "
            "인원·건수는 남녀 값을 더해 보고, 비율은 성별로만 제공됩니다."
        )
    return out


def other_versions(family: dict[str, Any]) -> str:
    """'계/남 · 기타, 전체 · 2015년판': 같은 대상의 다른 성별 · 다른 대상 · 다른 판"""
    current = family["current"]
    sexes = [m["sex"] for m in family["members"] if m["target"] == current["target"] and m["sex"]]
    targets = list(
        dict.fromkeys(m["target"] for m in family["members"] if m["target"] != current["target"])
    )
    parts = ["/".join(sexes), ", ".join(t for t in targets if t)]
    parts.append(", ".join(f"{e['period']}년판" for e in family["editions"]))
    return " · ".join(p for p in parts if p)


def notice(result: dict[str, Any]) -> str | None:
    """판 메모 + 불충족 조건 ('2024년 조건 불충족'). 판 메모가 연도를 설명하면 연도는 빼고"""
    notes = list(result.get("notes") or [])
    for key in result.get("failed") or []:
        if key == "year" and notes:
            continue
        notes.append(f"{check_label(key, result['slots'])} 조건을 충족하지 않습니다")
    return "; ".join(notes) or None


def answer_text(result: dict[str, Any], rec: dict[str, Any] | None) -> str:
    status = result["status"]
    if status in STATUS_MESSAGES:
        return STATUS_MESSAGES[status]
    if status == "ambiguous":
        lines = ["비슷한 통계표가 여러 개 있습니다. 어떤 자료를 찾으시나요?"]
        for n, o in zip(NUMBERS, result["options"], strict=False):
            lines.append(f"{n} {o['table_name']} ({o['survey_name']}, {o['edition']}년)")
        lines.append("번호나 이름으로 답해 주세요.")
        return "\n".join(lines)
    if status == "clarify":
        f = result["family"]
        axes = result["clarify_axes"]
        lines = [
            f"‘{f['family_name']}’ 통계({f['organization']}, {f['survey_name']})가 있습니다. "
            f"{len(axes)}가지만 확인할게요."
        ]
        for n, a in zip(NUMBERS, axes, strict=False):
            options = [f"{o}년" if a["axis"] == "edition" else o for o in a["options"]]
            lines.append(f"{n} {AXIS_LABELS[a['axis']]}: {' / '.join(options)}")
        lines.append("예) “혼인 귀화자, 여, 2010년” 또는 “아무거나”")
        return "\n".join(lines)

    slots = result["slots"]
    lines = [
        f"요청하신 통계는 ‘{rec['survey_nm']}’에서 확인할 수 있습니다.",
        f"추천 통계표: {rec['tbl_nm']} ({rec['org_nm']}, {rec['period']}년)",
    ]
    if rec["checks"]:
        marks = [f"{check_label(k, slots)} {'✔' if ok else '✘'}" for k, ok in rec["checks"].items()]
        lines.append("조건 확인: " + " · ".join(marks))
    if result.get("notice"):
        lines.append(f"확인 필요: {result['notice']}")
    if "target" in result.get("missing_axes", []) or slots.get("target_default"):
        lines.append("대상을 지정하지 않아 ‘전체’ 표를 안내합니다.")
    others = other_versions(rec["family"])
    if others:
        lines.append(f"같은 표의 다른 버전: {others}")
    lines += [f"참고: {n}" for n in rec["notes"]]
    lines.append(f"바로가기: {rec['url']}")
    return "\n".join(lines)


def respond(
    result: dict[str, Any],
    tables: list[dict[str, Any]],
    session_id: str,
    top_k: int = 3,
    trace: dict[str, Any] | None = None,
    debug: bool = False,
) -> dict[str, Any]:
    by_id = {t["tbl_id"]: t for t in tables}
    status = result["status"]
    slots = result["slots"]
    if status == "out_of_scope" and slots.get("parse_error"):
        status = result["status"] = "parse_error"
    result["notice"] = notice(result) if status in ("ok", "partial_match") else None

    recs: list[dict[str, Any]] = []
    clarify = None
    if status in ("ok", "partial_match"):
        chosen = by_id[result["table"]["tbl_id"]]
        family = [t for t in tables if t["family_id"] == chosen["family_id"]]
        recs.append(
            recommendation(1, chosen, family, result["table"]["checks"], result["score"], slots)
        )
        # 2순위부터는 다른 패밀리의 대표 표
        for i, o in enumerate(result.get("other_families", [])[: max(top_k - 1, 0)], start=2):
            t = by_id[o["tbl_id"]]
            fam = [r for r in tables if r["family_id"] == t["family_id"]]
            recs.append(recommendation(i, t, fam, o.get("checks", {}), o["score"], slots))
    elif status == "clarify":
        clarify = {
            "level": "member",
            "family_id": result["family"]["family_id"],
            "family_name": result["family"]["family_name"],
            "axes": result["clarify_axes"],
        }
    elif status == "ambiguous":
        clarify = {
            "level": "family",
            "families": [
                {
                    "family_id": o["family_id"],
                    "family_name": o["family_name"],
                    "tbl_id": o["tbl_id"],
                    "tbl_nm": o["table_name"],
                    "survey_nm": o["survey_name"],
                    "period": o["edition"],
                    "url": o["url"],
                }
                for o in result["options"]
            ],
        }

    response = {
        "session_id": session_id,
        "status": status,
        "answer": answer_text(result, recs[0] if recs else None),
        "parsed": {k: slots[k] for k in ("topic", "target", "conditions", "query_type")},
        "clarify": clarify,
        "recommendations": recs,
        "notice": result["notice"],
    }
    if debug:
        response["debug"] = {
            "candidates": result.get("candidates", []),
            "missing_axes": result.get("missing_axes", []),
            "fixes": slots.get("fixes", []),
            "parse_error": slots.get("parse_error", False),
            **(trace or {}),
        }
    return response
