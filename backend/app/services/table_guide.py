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
from typing import Any
from uuid import uuid4

from app.agent import graph, multi
from app.core.config import get_settings
from app.services import search, search_answer, search_log
from app.services import slots as slot_parser

logger = logging.getLogger(__name__)

# 수치로 답할 수 없어 통계표 찾기로 넘기는 상태
SEARCH_INSTEAD = {"unknown_indicator", "not_supported_question", "parse_error"}
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
        result = await handle(question, session_id, selections, trace)
        tables = await search.load_tables()
        response = search_answer.respond(result, tables, session_id, TOP_K, trace)
        by_indicator = indicator_tables(result.get("multi"), tables)
        if by_indicator:
            response["multi"] = by_indicator
        return response
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
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
                search_answer.recommendation(len(out) + 1, t, family, {}, c.get("score"), slots)
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
    """통계표 찾기 결과 → /ask 응답 (수치 없이 표 안내만)"""
    lines = [found["answer"]]
    if refused and found["status"] in ("ok", "partial_match"):
        lines.append("이 통계는 아직 수치를 바로 계산하지 못해 통계표를 안내합니다.")
    return {
        "status": "tables",
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
