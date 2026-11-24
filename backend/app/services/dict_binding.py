"""지표 사전 답의 잇기 기록 (B 1단계, docs/cell_binding_design_2026-11-23.md 9-3 · 10장)

지표 사전(사전 계산)은 질문의 지표 이름만 맞으면 답한다. 질문의 다른 조건("세쌍둥이 이상",
"상반기", "서울에서 경기로", "국적별")이 그 지표의 표에 이어지는지는 보지 않는다
(V2-008: 세쌍둥이 이상 출생아를 출생아 전체 254,341명으로 답했다).

BINDING_DICT_SHADOW=true면 지표 사전이 답한 뒤 같은 질문으로 통계표 찾기의 조건 추출을 돌리고,
지표 사전이 쓴 표에 그 조건을 잇는다 (지표 사전이 고른 항목 칸은 정해 둔다). 답은 바꾸지 않고
trace["dict_binding"]에 "그대로 답했을 것 / 넘겼을 것"과 잇지 못한 조건을 남긴다.
scripts/dict_binding_compare.py가 평가 결과의 정답 여부와 묶어 본다.
"""

import logging
import re
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.services import binding, indicators

logger = logging.getLogger(__name__)

KEEP, HAND_OFF = "그대로", "넘김"
# 계산 지표(총인구 · 고령인구 비율 등)는 주민등록인구 표의 이 항목으로 계산한다 (성별은 항목)
POP_ITEMS = {"계": "T2", "남": "T3", "여": "T4"}  # 총인구수 · 남자인구수 · 여자인구수
# 지표 사전이 스스로 맞추는 분류 (이 분류에 이은 조건은 사전이 반영한다)
HANDLED_DIM = re.compile(r"연령|성별|성\b|행정구역|시도|시군구|지역")
# 월 · 분기 · 반기 단위를 물음 (지표 사전은 연간 값으로 답한다, V2-021 '상반기')
SUB_YEAR = re.compile(r"\d{1,2}\s*월|상반기|하반기|\d\s*분기|분기별|월별")
SEX_CLASS = re.compile(r"^(성별?|남녀|성\s*별)$")
MONTHLY = re.compile(r"월\s*말|월별")  # 답이 월 값인가 ("2026년 10월 말 기준", "KOSIS 월별 자료")
# '○○에서 ○○(으)로' 옮김: 전출지 → 전입지 (지표 사전은 두 지역의 전입 · 전출 합계, V2-011)
FROM_TO = re.compile(r"\S+에서\s*\S+?(?:으로|로)\s*(?:이사|이동|옮|간|가|온|오|빠져)")


def _source(result: dict, parsed: dict) -> tuple[str | None, dict[int, str]]:
    """지표 사전이 쓴 표와 정해 둔 칸 {분류 번호: 코드}. 표를 모르면 (None, {})"""
    from app.services import qa

    src = qa.sources(result)
    tbl_id = src[0]["tbl_id"] if src else None
    sp = indicators.spec(parsed.get("indicator") or "") or {}
    if not tbl_id:
        return None, {}
    item = next(
        (s.get("item_id") for s in sp.get("sources") or [] if s.get("table") == tbl_id), None
    )
    if item is None and tbl_id == indicators.POP_TABLE:
        item = POP_ITEMS.get(parsed.get("sex") or "계", POP_ITEMS["계"])
    return tbl_id, ({0: item} if item else {})


def _ignored(b: "binding.Binding", tbl_id: str) -> list[str]:
    """잇기는 이었지만 지표 사전이 그 칸을 쓰지 않는 조건 (V2-074 '국적별': 잇기는 국적 분류 전체에
    이었는데 사전은 합계로 답했다). 항목(정해 둔 칸) · 연령 · 성별 · 지역 · 기간은 사전이 맞춘다"""
    from app.services import dynamic_indicator as di

    names = {d.no: d.name for d in di.schema(tbl_id)[0]}
    out = []
    for x in b.bound:
        if x.dim in (None, 0) or x.by in ("forced", "total", "default", "region", "period"):
            continue
        if x.by == "dim" or not HANDLED_DIM.search(names.get(x.dim, "")):
            out.append(
                f"{x.cond} '{x.value}' → {names.get(x.dim, x.dim)} (지표 사전이 쓰지 않는 칸)"
            )
    return out


async def _conditions(question: str) -> dict[str, Any]:
    """통계표 찾기와 같은 조건 추출 (HCX). 넘기기 꾸러미의 understanding 모양"""
    from app.services import search, table_guide
    from app.services import slots as slot_parser

    found = await slot_parser.extract(question, {})
    search.ground_conditions(found)
    return table_guide._understanding(found)


async def shadow(question: str, parsed: dict, result: dict) -> dict[str, Any] | None:
    """지표 사전 답(result) → 잇기 기록. 답이 아니거나 표를 모르면 None"""
    if result.get("status") != "ok" or not question:
        return None
    tbl_id, forced = _source(result, parsed)
    if not tbl_id:
        return None
    try:
        u = await _conditions(question)
        cond = dict(u.get("conditions") or {})
        # 성별 분류는 지표 사전이 맞춘다 (주민등록인구는 성별이 항목 '여자인구수'라 분류가 없다)
        cond["classes"] = [c for c in cond.get("classes") or [] if not SEX_CLASS.match(c)]
        slots = {
            "conditions": cond,
            "target": u.get("target"),
            "topic": u.get("topic"),
            "table_name": result.get("source") or "",
            "table_words": [result.get("source") or "", u.get("target")],
        }
        # 성별은 지표 사전이 맞춘다 (항목 '여자인구수', 조회 지표의 by_sex)
        q = {**parsed, "sex": None}
        b = await run_in_threadpool(binding.bind, tbl_id, question, q, slots, forced)
        ignored = await run_in_threadpool(_ignored, b, tbl_id)
    except Exception as e:  # 기록만 남기는 단계라 답에는 영향을 주지 않는다
        logger.exception("지표 사전 잇기 기록 실패: %s", tbl_id)
        return {"tbl_id": tbl_id, "error": f"{type(e).__name__}: {e}"}
    reasons = [
        f"{u.cond} '{u.value}': {u.why}" if u.value else f"{u.cond}: {u.why}" for u in b.unbound
    ]
    reasons += ignored
    basis = str(result.get("basis") or "")
    if SUB_YEAR.search(question) and not result.get("as_of") and not MONTHLY.search(basis):
        reasons.append("월 · 분기 단위를 물었는데 연간 값")
    if FROM_TO.search(question):
        reasons.append("'○○에서 ○○로' 옮긴 사람 (전출지 → 전입지)")
    out = b.to_dict()
    out["tbl_id"] = tbl_id
    out["reasons"] = reasons
    out["decision"] = HAND_OFF if reasons else KEEP
    out["forced"] = {str(k): v for k, v in forced.items()}
    out["understanding"] = {k: u.get(k) for k in ("topic", "target", "conditions")}
    return out
