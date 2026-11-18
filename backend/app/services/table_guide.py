"""한 번의 질문에 수치와 통계표를 함께: 수치 답변(qa)에 통계표 안내(search)를 붙인다

수치 답변과 통계표 찾기는 따로 만들어졌다 (실전 1: 지표 40개의 수치, 실전 2: 표 2,364개 안내).
화면에서는 질문 하나에 둘 다 나와야 하므로 /ask 한 곳에서 합친다. /search는 평가용으로 남긴다.

  수치로 답했다            답에 쓴 표를 표 카드로 붙인다 (표 메타데이터만 쓴다, HCX 없음)
  수치로 답할 수 없는 질문  통계표 찾기를 돌려 표 카드·선택지를 준다
  (지원하지 않는 지표, 통계와 관계없는 말, 해석 실패)
                          HCX 3회: 조건 추출, 계획, 최종 검토 (app/agent/multi.py).
                          지표 여러 개를 물었으면 지표마다 찾은 표를 multi 칸에 덧붙인다
  그 밖의 답변 불가        수치 쪽 안내 그대로 (연도·지역 되묻기, 자료 준비 중, 범위 밖)

질문마다 둘 다 돌리지 않는 이유: HCX 한도(분당 60회·6만 토큰) 안에서 질문 하나에 HCX를 세 번 부르면
몇 명만 써도 한도에 걸린다. 수치로 답할 수 있으면 그 표가 곧 안내할 표다.
"""

import logging
import re
from typing import Any
from uuid import uuid4

from app.agent import graph, multi
from app.core.config import get_settings
from app.services import search, search_answer, search_llm, search_log
from app.services import slots as slot_parser

logger = logging.getLogger(__name__)

# 수치로 답할 수 없어 통계표 찾기로 넘기는 상태
SEARCH_INSTEAD = {"unknown_indicator", "not_supported_question", "parse_error"}
# 지표 사전이 지표는 알아들었지만 자료가 없어 답하지
# 못한 상태. 다른 표에는 있을 수 있어 통계표 찾기 ·
# 바로 계산을 해 본다 (ask_session._search_after). 계산되지 않으면 사전의 안내를 그대로 준다.
#   missing_data        그 지역 단위 · 구분 자료가 사전의 표에 없음 ("시도 단위까지만")
#   out_of_range        사전의 표 수록 기간 밖 (추계값 제안 suggestion이 있으면 넘기지 않는다)
#   region_not_in_year  그해에 없던 지역 (행정구역 개편)
# 되묻기(지역 · 연도), 지역을 모름, 표 받는 중, 계산 오류는 넘기지 않는다 (2026-11-18 세진 결정)
SEARCH_AFTER = {"missing_data", "out_of_range", "region_not_in_year"}
# 통계표 찾기도 답하지 못한 상태: 제공 범위(인구 분야) 밖, 통계 질문이 아님.
# /ask 응답의 status도 이 값으로 둔다 (표 안내 "tables"와 구분해 거절로 센다)
REFUSED = ("out_of_scope", "not_statistics")
TOP_K = 3
MAX_CARDS = 3  # 답에 쓴 표가 여럿이면 (과거 연도용 표 등) 이만큼만 카드로


def new_search_session() -> str:
    return f"S_{uuid4().hex[:12]}"


async def cards_for(question: str, sources: list[dict]) -> list[dict]:
    """답에 쓴 표 → 표 카드 (통계표 찾기의 추천 카드와 같은 모양). 카탈로그에 없는 표는 뺀다"""
    ids = list(dict.fromkeys(s["tbl_id"] for s in sources if s.get("tbl_id")))
    if not ids:
        return []
    tables = await search.load_tables()
    by_id = {t["tbl_id"]: t for t in tables}
    slots = slot_parser.empty_slots(question)
    cards = []
    for tbl_id in ids:
        t = by_id.get(tbl_id)
        if t is None:
            continue
        family = [r for r in tables if r["family_id"] == t["family_id"]]
        card = search_answer.recommendation(len(cards) + 1, t, family, {}, None, slots)
        basis = next((s.get("basis") for s in sources if s.get("tbl_id") == tbl_id), None)
        card["basis"] = basis
        cards.append(card)
        if len(cards) >= MAX_CARDS:
            break
    return cards


async def find_tables(
    question: str,
    session_id: str,
    selections: dict[str, Any] | None,
    user_id: int | None,
) -> dict:
    """통계표 찾기 한 턴. 되묻기 중이면 이어서 처리한다.

    /search(dialog.handle)와 같은 처리를 LangGraph 흐름(app/agent)으로 돌린다.
    새 질문이면 계획이 질문을 지표로 나누고, 지표가 여럿이면 지표마다 찾은 표를
    응답의 multi에 덧붙인다 (SEARCH_PLAN=false면 계획 없이 기존과 같은 결과만)"""
    trace: dict = {}
    result, response, error = None, None, None
    handle = multi.handle if get_settings().search_plan else graph.handle
    try:
        result = await handle(question, session_id, selections, trace, user_id=user_id)
        session_id = trace.get("search_session_id", session_id)
        tables = await search.load_tables()
        response = search_answer.respond(result, tables, session_id, TOP_K, trace)
        by_indicator = indicator_tables(result.get("multi"), tables)
        if by_indicator:
            response["multi"] = by_indicator
        try:  # 뒤 단계(수치 계산)에 넘길 꾸러미. 만들다 실패해도 표 안내는 그대로 준다
            response["handoff"] = build_handoff(result, response, tables)
        except Exception:
            logger.exception("넘기기 꾸러미(handoff) 만들기 실패")
            response["handoff"] = None
        return response
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        session_id = trace.get("search_session_id", session_id)
        try:
            await search_log.write(
                question, session_id, result, response, trace, None, error, user_id=user_id
            )
        except Exception:  # 기록 실패가 답변을 막지 않게
            logger.exception("검색 요청 기록 실패")


def indicator_tables(multi_result: dict | None, tables: list[dict]) -> dict | None:
    """지표가 여럿인 질문의 지표별 표 카드 (추천 카드와 같은 모양). 지표가 하나면 None

    화면은 아직 이 칸을 쓰지 않는다. 지표를 잘못 나눴을 때를 대비해 질문 전체 검색의
    상위 표(whole)도 함께 준다"""
    if not multi_result or multi_result.get("mode") != "multi":
        return None
    by_id = {t["tbl_id"]: t for t in tables}

    def cards(candidates: list[dict], question: str) -> list[dict]:
        slots = slot_parser.empty_slots(question)
        out = []
        for c in candidates:
            t = by_id.get(c["tbl_id"])
            if t is None:
                continue
            family = [r for r in tables if r["family_id"] == t["family_id"]]
            out.append(
                search_answer.recommendation(
                    len(out) + 1, t, family, c.get("checks") or {}, c.get("score"), slots
                )
            )
        return out

    plan = multi_result.get("plan") or {}
    return {
        "indicators": [
            {
                "indicator": ind.get("span"),
                "merged": ind.get("merged_spans") or [],
                "question": ind.get("question"),
                "conditions": ind.get("conditions") or {},
                "tables": cards(ind.get("candidates") or [], ind.get("question") or ""),
            }
            for ind in multi_result.get("indicators") or []
        ],
        "whole": cards(multi_result.get("whole") or [], ""),
        "combine": plan.get("combine") or {},
        "out_of_scope": plan.get("out_of_scope") or [],
    }


def tables_answer(found: dict, refused: dict | None = None) -> dict:
    """통계표 찾기 결과 → /ask 응답 (수치 없이 표 안내만).
    표 찾기도 범위 밖·통계 질문 아님으로 끝났으면 status는 그 값 (REFUSED)"""
    lines = [found["answer"]]
    if refused and found["status"] in ("ok", "partial_match"):
        lines.append("이 통계는 아직 수치를 바로 계산하지 못해 통계표를 안내합니다.")
    return {
        "status": found["status"] if found["status"] in REFUSED else "tables",
        "answer": "\n".join(lines),
        "parsed": (refused or {}).get("parsed") or {},
        "result": {"status": "tables", "search_status": found["status"]},
        "sources": [],
        "chart": None,
        "tables": found,
        "trace": {},
    }


def waiting_choice(found: dict | None) -> bool:
    """통계표 찾기가 고를 것을 물었는가 (다음 말을 그 답으로 읽는다)"""
    return bool(found and found.get("status") in ("clarify", "ambiguous"))


# ---- 넘기기 꾸러미 (계약_초안 v1, 2026-11-17)
# ------------------------------------------
# 통계표 찾기가 한 일을 뒤 단계(수치 계산)에 한 번에 넘긴다. 지금 응답(found)의 칸은 그대로 두고
# found["handoff"]만 더한다. 순위 · 고른 표는 바꾸지
# 않는다 (이미 계산된 값을 옮겨 담기만 한다).
# handoff = {version, question, status, understanding, targets[], combine, clarify, llm, ambiguity}
# targets[] = 지표마다 {span, merged_spans, scope,
# role, conditions, question, candidates[], together}
#   candidates[] = 순위대로 최대 5개 {rank, tbl_id, org_id, tbl_nm, family_id, start_year, end_year,
#                                   target, sex, score, source, picked, review, checks, editions,
#                                   members, notes}

HANDOFF_VERSION = 1
MAX_CANDIDATES = 5
COMPUTABLE = ("ok", "partial_match")  # 표를 골랐다 (후보를 넘긴다). 그 밖은 targets가 빈 목록
REGION_SUFFIX = re.compile(r"(특별자치시|특별자치도|특별시|광역시|도|시|군|구)$")
SEX_SAID = {"남": re.compile(r"남(자|성|녀)|성별"), "여": re.compile(r"여(자|성)|남녀|성별")}
YEAR_SAID = re.compile(r"(?:19|20)\d{2}(?!\d|\s*세대)")  # "2030세대"는 연도가 아니다


def build_handoff(
    result: dict[str, Any], response: dict[str, Any], tables: list[dict[str, Any]]
) -> dict[str, Any]:
    """통계표 찾기의 내부 결과(result) → 넘기기 꾸러미. response는 respond가 만든 응답"""
    slots = result.get("slots") or {}
    question = slots.get("question") or ""
    status = response.get("status")
    by_id = {t["tbl_id"]: t for t in tables}
    families: dict[str, list[dict[str, Any]]] = {}
    for t in tables:
        families.setdefault(t["family_id"], []).append(t)
    multi_result = result.get("multi") or {}
    plan = multi_result.get("plan") or {}
    understanding = _understanding(slots)

    targets: list[dict[str, Any]] = []
    if status in COMPUTABLE and (result.get("table") or {}).get("tbl_id") in by_id:
        picked_id = result["table"]["tbl_id"]
        if multi_result.get("mode") == "multi":
            targets = [
                _indicator_target(ind, understanding, by_id, families, picked_id)
                for ind in multi_result.get("indicators") or []
            ]
        else:
            targets = [_single_target(result, understanding, by_id, families, slots)]

    combine = plan.get("combine") or {}
    pick = result.get("llm_pick") or {}
    return {
        "version": HANDOFF_VERSION,
        "question": question,
        "status": status,
        "understanding": understanding,
        "targets": targets,
        "combine": {
            "op": combine.get("op") or "none",
            "note": combine.get("note"),
            "out_of_scope": list(plan.get("out_of_scope") or []),
        },
        "clarify": response.get("clarify"),
        "llm": _llm_used(multi_result, pick),
        "ambiguity": None,  # 뜻이 갈리는 말 알아채기 (1차 범위 밖, 자리만)
    }


def _understanding(slots: dict[str, Any]) -> dict[str, Any]:
    """탐색이 질문을 어떻게 읽었나 (조건 추출 + 보정 뒤)"""
    cond = slots.get("conditions") or {}
    return {
        "topic": slots.get("topic"),
        "search_query": slots.get("search_query"),
        "target": slots.get("target"),
        "conditions": {
            "region": cond.get("region"),
            "region_level": cond.get("region_level"),
            "year": cond.get("year"),
            "year_end": cond.get("year_end"),
            "sex": cond.get("sex"),
            "items": list(cond.get("items") or []),
            "classes": list(cond.get("classes") or []),
        },
        "age": slots.get("age"),
        "exclude": list(slots.get("exclude") or []),
        "spouse": slots.get("spouse"),
        "projection": bool(slots.get("projection")),
        "time_ref": slots.get("time_ref"),
        "query_type": slots.get("query_type"),
        "dropped": list(slots.get("dropped_conditions") or []),
        "origin": _origin(slots),
    }


def _origin(slots: dict[str, Any]) -> dict[str, str]:
    """지역 · 연도 · 성별 조건이 어디서 왔나: question(질문에 그 말이 있음) / default(규칙이 채움) /
    guess(질문에 그 말이 없는데 조건이 있음). 1차는 글자 대조로 어림한다"""
    question = slots.get("question") or ""
    said = search.norm(question)
    cond = slots.get("conditions") or {}
    out: dict[str, str] = {}
    if region := cond.get("region"):
        stems = {search.norm(region), search.norm(REGION_SUFFIX.sub("", region))}
        out["region"] = "question" if any(s and s in said for s in stems) else "guess"
    if cond.get("year") is not None:
        if slots.get("time_ref") or YEAR_SAID.search(question):
            out["year"] = "question"
        else:
            out["year"] = "default"
    if (sex := cond.get("sex")) in SEX_SAID:
        out["sex"] = "question" if SEX_SAID[sex].search(question) else "guess"
    return out


def _candidate(
    rank: int,
    t: dict[str, Any],
    family: list[dict[str, Any]],
    by_id: dict[str, dict[str, Any]],
    checks: dict,
    score: float | None,
    slots: dict[str, Any],
    source: str,
    picked: bool,
    review: dict[str, Any] | None,
) -> dict[str, Any]:
    """표 하나 → 후보. 판 · 멤버 · 주석은 추천 카드(search_answer.recommendation)와 같은 방법으로"""
    card = search_answer.recommendation(rank, t, family, checks, score, slots)
    return {
        "rank": rank,
        "tbl_id": t["tbl_id"],
        "org_id": t["org_id"],
        "tbl_nm": t["table_name"],
        "family_id": t["family_id"],
        "start_year": t.get("start_year"),  # 수록 기간 (다른 판을 고를 때 쓴다)
        "end_year": t.get("end_year"),
        "target": t.get("target"),  # 표의 대상 · 성별 (대상 조건 검사에 쓴다)
        "sex": t.get("sex"),
        "score": card["score"],
        "source": source,
        "picked": picked,
        "review": review,
        "checks": dict(checks or {}),
        "editions": [
            {
                "tbl_id": e["tbl_id"],
                "period": e["period"],
                "start_year": by_id.get(e["tbl_id"], {}).get("start_year"),
                "end_year": by_id.get(e["tbl_id"], {}).get("end_year"),
            }
            for e in card["family"]["editions"]
        ],
        "members": [
            {"tbl_id": m["tbl_id"], "target": m["target"], "sex": m["sex"]}
            for m in card["family"]["members"]
        ],
        "notes": card["notes"],
    }


def _review_of(
    result: dict[str, Any], picked: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """검토(LLM)가 고른 표에 대해 낸 판단. 검토가
    없었거나, 검토가 고른 표가 탐색이 최종으로 고른 표와
    다른 묶음이면(예: HCX는 거르기만 하고 표를 바꾸지 않는다) None.
    같은 묶음의 다른 판 · 멤버를 골랐으면 같은 판단으로 본다 (판 · 멤버는 그 뒤 규칙이 고른다)"""
    pick = result.get("llm_pick") or {}
    raw = pick.get("raw") or {}
    chosen = pick.get("pick")
    if not raw or not chosen:
        return None
    if chosen != picked["tbl_id"]:
        other = by_id.get(chosen)
        if other is None or other["family_id"] != picked["family_id"]:
            return None
    return {"match": raw.get("match"), "reason": raw.get("reason"), "llm": pick.get("llm")}


def _single_target(
    result: dict[str, Any],
    understanding: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    families: dict[str, list[dict[str, Any]]],
    slots: dict[str, Any],
) -> dict[str, Any]:
    """지표 하나 질문: 탐색이 고른 표가 1위, 그 뒤로 질문 전체 검색의 다른 묶음 (최대 5개).
    질문 전체 검색 상위 5개(multi.whole)가 없으면(되묻기 답 턴, 계획 끔)
    다른 묶음 대표(other_families)"""
    chosen = by_id[result["table"]["tbl_id"]]
    out = [
        _candidate(
            1,
            chosen,
            families.get(chosen["family_id"], [chosen]),
            by_id,
            result["table"].get("checks") or {},
            result.get("score"),
            slots,
            "whole",
            True,
            _review_of(result, chosen, by_id),
        )
    ]
    rest = (result.get("multi") or {}).get("whole") or result.get("other_families") or []
    seen = {chosen["family_id"]}
    for c in rest:
        t = by_id.get(c["tbl_id"])
        if t is None or t["family_id"] in seen:
            continue
        seen.add(t["family_id"])
        checks = c.get("checks")
        if checks is None:  # 예전 모양의 후보: 조건 확인을 여기서 한다 (검색과 같은 함수)
            checks = search.check(t, slots) if slots.get("conditions") else {}
        out.append(
            _candidate(
                len(out) + 1,
                t,
                families.get(t["family_id"], [t]),
                by_id,
                checks,
                c.get("score"),
                slots,
                "whole",
                False,
                None,
            )
        )
        if len(out) >= MAX_CANDIDATES:
            break
    return {
        "span": None,
        "merged_spans": [],
        "scope": "in",
        "role": None,
        "conditions": understanding["conditions"],
        "question": slots.get("question") or "",
        "candidates": out,
        "together": list(result.get("together") or []),
    }


def _indicator_target(
    ind: dict[str, Any],
    understanding: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    families: dict[str, list[dict[str, Any]]],
    picked_id: str,
) -> dict[str, Any]:
    """지표가 여럿인 질문의 지표 하나: 지표별 검색의 표 묶음 상위 5개.
    지표마다 검토(LLM)를 하지 않으므로 review는 없다. 질문 전체 검색이 고른 표와 같은 표면 picked"""
    question = ind.get("question") or ""
    slots = slot_parser.empty_slots(question)
    out = []
    for c in ind.get("candidates") or []:
        t = by_id.get(c["tbl_id"])
        if t is None:
            continue
        out.append(
            _candidate(
                len(out) + 1,
                t,
                families.get(t["family_id"], [t]),
                by_id,
                c.get("checks") or {},
                c.get("score"),
                slots,
                "indicator",
                t["tbl_id"] == picked_id,
                None,
            )
        )
        if len(out) >= MAX_CANDIDATES:
            break
    return {
        "span": ind.get("span"),
        "merged_spans": list(ind.get("merged_spans") or []),
        "scope": ind.get("scope") or "in",
        "role": ind.get("role"),
        "conditions": _plan_conditions(ind.get("conditions") or {}, understanding["conditions"]),
        "question": question,
        "candidates": out,
        "together": [],
    }


def _plan_conditions(cond: dict[str, Any], whole: dict[str, Any]) -> dict[str, Any]:
    """계획의 지표별 조건(region_level · year_from · year_to · sex · age) → understanding 조건 모양.
    지역 이름 · 항목 · 분류는 계획에 없어 지역만 질문 전체의 것을 쓴다. age는 계획의 글자 그대로"""
    year, year_end = cond.get("year_from"), cond.get("year_to")
    if year is None and year_end is not None:
        year, year_end = year_end, None
    return {
        "region": whole.get("region"),
        "region_level": cond.get("region_level"),
        "year": year,
        "year_end": year_end if year_end != year else None,
        "sex": cond.get("sex") if cond.get("sex") in ("남", "여") else None,
        "items": [],
        "classes": [],
        "age_text": cond.get("age"),
    }


def _llm_used(multi_result: dict[str, Any], pick: dict[str, Any]) -> dict[str, str | None]:
    """단계마다 판단한 LLM. 검토 · 함께 볼 표는 실제로 답한 쪽이 기록돼 있다.
    조건 추출 · 계획은 답한 쪽을 남기지 않아 설정 기준으로 적는다 (Gemma가 잠시 꺼져 HCX로 넘어간
    상태면 hcx)"""
    configured = "hcx" if search_llm.falling_back() else search_llm.provider()
    plan = multi_result.get("plan") or {}
    together = pick.get("together") or {}
    return {
        "slots": configured,
        "plan": configured if multi_result and not plan.get("error") else None,
        "review": pick.get("llm") if pick.get("raw") else None,
        "together": "gemma" if together and not together.get("error") else None,
    }
