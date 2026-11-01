"""검색 뒤 HCX-007 최종 검토 (실전 2, 실험실 §56-4·§61-3): 상위 5 후보 중 질문에 맞는 표 하나를 고르고,
어느 후보도 질문 주제를 다루지 않으면 범위 밖으로 돌린다.

- HCX-007, 생각 끄기(thinking none), Structured Outputs(responseFormat json + 후보 tbl_id enum)라 JSON이 깨지지 않고
  약 1.5초·900토큰. 실험실: 범위 밖 재현율 30/30, 거짓 거절 1/102, 새 테스트셋 흐름 68→74.
- 고른 표의 패밀리가 1위와 다르면 그 패밀리로 멤버·판 선택을 다시 한다(resolve). 되묻기(clarify)가 필요한 결과는
  건드리지 않는다. 호출이 실패하면 검색 결과를 그대로 둔다.
- 모드(LLM_PICK_MODE): filter = 거르기만 / pick = 고른 표로 패밀리 교체 / resolve(기본) = 확정 결과는 거르기만,
  멤버 되묻기(clarify) 상태에서는 고른 표로 멤버·판까지 확정 (되묻기 횟수가 준다). 패밀리 모호는 1위 유지.
- 설정 LLM_PICK=false 로 끈다.
"""

import json
import logging
import re
import time
from typing import Any

from app.core.config import get_settings
from app.services.hcx_client import HcxClient

logger = logging.getLogger(__name__)

MODEL = "HCX-007"
TOP_K = 5
SYSTEM = """당신은 통계청 KOSIS 통계표 안내 챗봇의 최종 검토자입니다. 사용자 질문과 검색된 후보 통계표 목록(최대 5개)을 받습니다.
각 후보에는 표 이름, 조사, 수록 기간, 지역 단위, 분류(축)와 값 예시, 항목이 있습니다.

할 일:
1. 질문에 가장 잘 맞는 후보 하나를 고릅니다. 기준: 묻는 대상·조건(연도, 지역 단위, 성별, 분류)을 실제로 담고 있는가, 묻는 값(수/비율/평균)이 항목에 있는가, 수록 기간이 질문 시점을 포함하는가. 같은 내용이면 수록 기간이 길고 최신인 표를 고릅니다.
2. 고른 표로 질문에 답할 수 있으면 answerable=true. 후보 중 어느 것도 질문의 주제를 다루지 않으면(예: 집값, 월급, 날씨, 신청 방법) pick="none", answerable=false.
   주제가 비슷해 보여도 질문이 묻는 값(예: 학생 수, 자살자 수, 흡연율, 의사 수, 빈집 수, 연금 수급자 수)이 항목·분류에 없으면 답할 수 없는 것입니다. 수십 년 전 한 해만 있는 표로 요즘 질문에 답하지 마세요.
후보 밖의 표 ID를 만들지 마세요. 설명 없이 JSON만: {"pick": "<후보의 tbl_id 또는 none>", "answerable": true|false, "reason": "<한 문장>"}"""


def cand_text(t: dict[str, Any], i: int) -> str:
    cls = "; ".join(
        f"{c['name']}({', '.join((c.get('sample_values') or [])[:4])})" for c in (t["classifications"] or [])[:4]
    )
    items = ", ".join(x["name"] for x in (t["items"] or [])[:8])
    return (
        f"[{i}] tbl_id={t['tbl_id']} | {t['table_name']} | 조사: {t['survey_name']} | 수록: {t['period']} | "
        f"지역 단위: {', '.join(t['region_levels'] or []) or '전국'} | 분류: {cls} | 항목: {items}"
    )


def parse(raw: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", re.sub(r"```(?:json)?", "", raw), re.S)
    if not m:
        return None
    try:
        o = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return o if isinstance(o, dict) else None


def norm_pick(parsed: dict[str, Any] | None, cands: list[str]) -> str | None:
    """pick 값을 후보 ID로. none/빈 값이면 None, 모르는 값이면 '__unknown__'"""
    if not parsed:
        return "__unknown__"
    s = str(parsed.get("pick") or "").strip()
    if s.lower() in ("", "null", "none"):
        return None
    for c in cands:
        if c in s:
            return c
    return "__unknown__"


async def _ask(question: str, cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    user = f"질문: {question}\n\n후보:\n" + "\n".join(cand_text(t, i + 1) for i, t in enumerate(cands))
    ids = [t["tbl_id"] for t in cands]
    result = await HcxClient().chat(
        MODEL,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        thinking={"effort": "none"},
        maxCompletionTokens=300,
        responseFormat={
            "type": "json",
            "schema": {
                "type": "object",
                "properties": {
                    "pick": {"type": "string", "enum": [*ids, "none"]},
                    "answerable": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["pick", "answerable", "reason"],
            },
        },
    )
    return parse(result.content)


async def review(
    question: str, result: dict[str, Any], tables: list[dict[str, Any]], trace: dict[str, Any]
) -> dict[str, Any]:
    """검색 결과에 LLM 검토를 얹는다. 후보가 없거나 되묻기면 그대로"""
    if not get_settings().llm_pick:
        return result
    if result.get("status") not in ("ok", "partial_match", "ambiguous", "clarify"):
        return result
    cand_ids = [c["tbl_id"] for c in (result.get("candidates") or [])[:TOP_K]]
    by = {t["tbl_id"]: t for t in tables}
    cands = [by[i] for i in cand_ids if i in by]
    if not cands:
        return result
    start = time.perf_counter()
    try:
        parsed = await _ask(question, cands)
    except Exception as e:  # 검토 실패는 검색 결과를 바꾸지 않는다
        logger.warning("LLM 검토 실패 → 검색 결과 그대로: %s: %s", type(e).__name__, e)
        trace["llm_pick"] = {"error": f"{type(e).__name__}: {e}"}
        return result
    pick = norm_pick(parsed, cand_ids)
    trace["llm_pick"] = {"raw": parsed, "pick": pick, "ms": round((time.perf_counter() - start) * 1000)}
    answerable = bool(parsed.get("answerable")) if parsed else True
    if pick is None or (pick != "__unknown__" and not answerable):
        return {
            **result,
            "status": "out_of_scope",
            "reason": "검토 결과 후보 통계표가 질문의 주제를 다루지 않습니다"
            + (f" ({parsed.get('reason')})" if parsed and parsed.get("reason") else ""),
            "llm_pick": trace["llm_pick"],
        }
    if pick == "__unknown__":
        return {**result, "llm_pick": trace["llm_pick"]}
    mode = get_settings().llm_pick_mode
    # 멤버 되묻기(clarify)만 LLM 이 확정한다. 패밀리 모호(ambiguous)는 1위 패밀리를 두는 편이 나았음
    # (레포 측정 §68-4: LLM 이 같은 조사의 형제 패밀리로 바꿔 8건 손실, 얻은 건 1건)
    undecided = result["status"] == "clarify"
    if mode == "filter" or (mode == "resolve" and not undecided):
        return {**result, "llm_pick": trace["llm_pick"]}
    picked = by[pick]
    current = (result.get("family") or {}).get("family_id") or (result.get("table") or {}).get("family_id")
    if picked["family_id"] == current and not undecided:
        return {**result, "llm_pick": trace["llm_pick"]}
    from app.services import search as se  # 순환 참조를 피해 여기서 읽는다

    slots = dict(result["slots"])
    if undecided:  # 되묻기 대신 LLM 이 고른 표로 멤버·판까지 확정
        slots["tbl_id"] = pick
    redone = se.resolve(tables, slots, picked["family_id"], allow_clarify=not undecided)
    redone["candidates"] = result.get("candidates", [])
    redone["other_families"] = result.get("other_families", [])
    redone["llm_pick"] = trace["llm_pick"]
    return redone
