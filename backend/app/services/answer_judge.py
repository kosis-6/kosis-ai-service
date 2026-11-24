"""답 판정: 답이 나간 뒤 Gemma 1회가 그 답이 질문에 맞는지 본다 (ANSWER_JUDGE=true, 기본 끔)

/ask 는 답을 계산한 뒤에는 그 답이 질문과 어긋나는지 다시 보지 않았다. 11/22 채점에서 답해야 하는
문항의 오답 104 중 69가 '답했지만 틀린' 것이었다 (실패로 끝난 것은 약 30). 그래서 성공한 답도
판정한다. 사전 지표로 답한 것과 통계표에서 바로 계산한 것 모두.

이 모듈은 판정만 한다 (다시 시도는 ask_session._judged):
  describe   판정에 보여 줄 답 요약 (지표 · 통계표 · 계산 · 지역 · 형태 · 기간). 숫자는 넣지 않는다
  time_read  코드가 질문의 시점 표현을 어떻게 읽었는지 (qa 의 time_refs)
  verdict    Gemma 에 물어 {"reason", "missing", "verdict": ok / retry / refuse}. 실패하면 None

Gemma 주소(GEMMA_BASE_URL)가 없으면 판정하지 않는다: HCX-007 은 틀린 답을 거의 다 통과시켰다
(app/prompts/answer_judge.py). 판정이 실패해도 답은 그대로 나간다.
"""

import logging
import time
from datetime import date
from typing import Any

from app.core.config import get_settings
from app.prompts.answer_judge import REASONS, SYSTEM
from app.services import llm_calls, llm_json, search_llm

logger = logging.getLogger(__name__)

MODEL = "HCX-007"  # GemmaClient 는 GEMMA_MODEL 로 바꿔 보낸다
MAX_TOKENS = 200
MAX_PARTS = 4  # 여러 값으로 나눠 답한 경우 요약에 넣는 수
VERDICTS = ("ok", "retry", "refuse")
SCHEMA = {
    "type": "object",
    "properties": {
        "reason": {"type": "string", "enum": REASONS},
        "missing": {"type": "string"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
    },
    "required": ["reason", "missing", "verdict"],
}


def enabled() -> bool:
    settings = get_settings()
    return settings.answer_judge and bool(settings.gemma_base_url.strip())


def leaves(result: dict[str, Any]) -> list[dict[str, Any]]:
    """여러 값으로 나눠 답한 결과(split)의 낱개 결과들. 나누지 않았으면 그 결과 하나"""
    if result.get("split"):
        return [x for part in result["split"] for x in leaves(part)]
    return [result]


def _shape(p: dict[str, Any], top: int | None) -> list[str]:
    """답의 형태(와 기간). 무엇을 주는지 적어야 판정이 '변화를 물었는데 값만 줬다'고 보지 않는다"""
    notes = [str(n) for n in p.get("notes") or []]
    if p.get("ranking"):
        shown = top or len(p["ranking"])
        names = ", ".join(str(x.get("region")) for x in p["ranking"][:3])
        return [
            f"형태: {p.get('level') or ''} 순위 ({p.get('order') or '높은 순'}), "
            f"전체 {p.get('total_regions')}곳 중 상위 {shown}곳을 답함 (위에서부터 {names})"
        ]
    if p.get("series"):
        years = [x["year"] for x in p["series"]]
        rate = " · 증감률" if "rate" in (p.get("change") or {}) else ""
        this_year = " (올해 값은 월별 최신 달)" if years[-1] >= date.today().year else ""
        return [
            f"형태: 연도별 값과 처음 → 끝 변화량{rate}",
            f"기간: {years[0]}~{years[-1]}년 ({len(years)}개 연도){this_year}",
        ]
    if p.get("values") and p.get("highest"):
        values = p["values"] if isinstance(p["values"], list) else []
        regions = ", ".join(str(v.get("region")) for v in values[:4])
        ratio = " + 몇 배" if p.get("ratio") else ""
        return [f"형태: 지역 비교 ({regions}): 값마다 + 차이{ratio}"]
    if cats := p.get("categories"):
        # 값이 큰 순으로 보여 준다: 분류 순위("어느 나라 사람이 제일 많아")의 답은 categories 에
        # 모든 구분이 표 순서대로 들어 있어, 앞 다섯 개만 보이면 판정이 순위를 못 뽑았다고 봤다
        with_value = [c for c in cats if isinstance(c.get("value"), (int, float))]
        ordered = sorted(with_value, key=lambda c: c["value"], reverse=True) or list(cats)
        if top:
            # 답한 만큼만 적는다 (판정이 수를 센다: 다섯 개를 적으면 "3위까지인데 5개"로 막았다)
            shown = ", ".join(str(c.get("label")) for c in ordered[:top])
            return [
                f"형태: 구분별 순위 (값이 큰 순), {len(cats)}개 중 상위 {top}개를 답함 ({shown})"
            ]
        labels = ", ".join(str(c.get("label")) for c in ordered[:5])
        return [f"형태: 구분별 값, 값이 큰 순으로 ({labels})"]
    if members := p.get("members"):
        # 여러 지역 · 여러 지표를 더한 값 (qa._sum_regions · _sum_indicators). "지역: A + B"만
        # 보이면 판정이 "더해야 함"이라고 막았다 (11/24): 더했다는 것을 말로 적는다
        if p.get("months"):
            # 달 묶음 합 (lookup.month_sum): 달 이름을 넷만 적으면 판정이 "1~4월만 더함"으로 봤다
            m0, m1 = p["months"][:2]
            return [f"형태: 합계 — {m0}~{m1}월 {len(members)}개 달의 값을 모두 더한 값"]
        names = ", ".join(str(m.get("region")) for m in members[:6])
        more = f" 외 {len(members) - 6}개" if len(members) > 6 else ""
        return [f"형태: 합계 — {names}{more}를 더한 값 ({len(members)}개)"]
    if total := next((n for n in notes if n.endswith("합계")), None):
        return [f"형태: 합계 ({total[:40]})"]
    return ["형태: 값 하나"]


def _part(p: dict[str, Any], top: int | None) -> str:
    one = [f"지표: {p.get('indicator')}"]
    if p.get("source"):
        one.append(f"통계표: {p['source']}")
    if p.get("formula"):
        one.append(f"계산: {p['formula']}")
    if p.get("region"):
        one.append(f"지역: {p['region']}")
    one += _shape(p, top)
    if not p.get("series") and p.get("year"):
        one.append(f"연도: {p['year']}년" + (f" ({p['as_of']})" if p.get("as_of") else ""))
    if p.get("sex") and p["sex"] != "계":
        one.append(f"성별: {p['sex']}")
    if p.get("ages"):
        one.append(f"연령: {p['ages']}")
    if p.get("unit"):
        one.append(f"단위: {p['unit']}")
    notes = " ".join(str(n) for n in p.get("notes") or [])
    formula = str(p.get("formula") or "")
    if "부부 단위" in notes:
        one.append("참고: 부부 단위 건수라 남자 칸 값이 전체 건수와 같음")
    elif "남편" in formula and "아내" in formula:
        # 표 경로가 부부 혼인 · 이혼 표에서 남편 쪽 · 아내 쪽 총계 칸을 함께 고른 답: 판정이
        # "둘 중 하나를 고르거나 합쳐야 함"으로 막았다 (11/24 평가, 51초). 둘은 같은 전체 건수다
        one.append("참고: 부부 단위 표라 남편 쪽과 아내 쪽 총계는 같은 전체 건수 (더하지 않음)")
    if "장래추계" in notes:
        one.append("참고: 장래추계 통계표의 추계값")
    return " / ".join(one)


def describe(result: dict[str, Any] | None, parsed: dict[str, Any] | None = None) -> str | None:
    """판정에 보여 줄 답 요약. 값이 있는 답이 아니면 None"""
    if not result or result.get("status") != "ok":
        return None
    parts = leaves(result)
    top = (parsed or {}).get("top")
    lines = [_part(p, top) for p in parts[:MAX_PARTS]]
    if len(parts) > 1:
        if len({p.get("sex") for p in parts}) > 1:
            how = "남 · 여 따로"
        elif len({p.get("region") for p in parts}) > 1:
            how = "지역마다"
        elif len({p.get("indicator") for p in parts}) > 1:
            how = "지표마다"
        else:
            how = "여러 값"
        lines.insert(0, f"(답을 {how} 계산해 함께 보여 줌: {len(parts)}개)")
        if v := result.get("verdict"):
            # which_larger (multi_indicator._larger): 어느 쪽이 큰지 판정까지 한 답
            lines.append(f"형태: 두 값 비교 — 큰 쪽 판정 ({v.get('line')})")
        elif (pair := _two_values(parts)) is not None:
            # 남 · 여 · 지역 둘을 따로 계산한 답: 답 문장은 "여성이 N명 더 많습니다"를 붙이지만
            # 결과에는 없어 판정이 "비교해야 함"으로 막았다. 큰 쪽은 코드가 세어 요약에만 적는다
            lines.append(pair)
        if notes := [str(n) for n in result.get("notes") or []]:
            # 지표끼리 비교한 답의 "A가 B보다 N명 많습니다" 같은 줄 (multi_indicator._side_by_side)
            lines.append("함께 적은 것: " + " / ".join(notes[:3]))
    return "\n".join(lines)


def _two_values(parts: list[dict[str, Any]]) -> str | None:
    """값 두 개를 따로 답한 결과(남 · 여, 지역 둘)의 큰 쪽 · 차이 한 줄. 아니면 None"""
    if len(parts) != 2 or any(not isinstance(p.get("value"), (int, float)) for p in parts):
        return None
    a, b = parts
    if a.get("unit") != b.get("unit"):
        return None

    def label(p: dict[str, Any]) -> str:
        if p.get("sex") in ("남", "여") and a.get("sex") != b.get("sex"):
            return {"남": "남성", "여": "여성"}[p["sex"]]
        return str(p.get("region") or p.get("indicator") or "값")

    if a["value"] == b["value"]:
        return f"형태: 두 값 비교 — {label(a)}와 {label(b)}가 같음"
    hi, lo = (a, b) if a["value"] > b["value"] else (b, a)
    gap = hi["value"] - lo["value"]
    gap_text = f"{gap:,}" if float(gap).is_integer() else f"{round(gap, 2):,}"
    return f"형태: 두 값 비교 — 큰 쪽 {label(hi)} (차이 {gap_text}{hi.get('unit') or ''})"


def time_read(parsed: dict[str, Any] | None, result: dict[str, Any] | None) -> str:
    """코드가 질문의 시점 표현을 읽은 결과 한 줄 ("'작년' → 2025년"). 읽은 것이 없으면 빈 문자열.
    끝을 말하지 않은 기간은 실제로 쓴 마지막 해를 적는다: 올해 월별 값이 있는 통계는 올해까지 세므로
    '2021년부터'라고만 적으면 실제 기간(2022~2026)과 어긋나 보였다 (11/22)"""
    ends = [p["series"][-1]["year"] for p in leaves(result or {}) if p.get("series")]
    last = max(ends) if ends else None
    said = []
    for ref in (parsed or {}).get("time_refs") or []:
        expr = ref.get("expr")
        if ref.get("year"):
            said.append(f"'{expr}' → {ref['year']}년")
        elif ref.get("start") and ref.get("end"):
            said.append(f"'{expr}' → {ref['start']}~{ref['end']}년")
        elif last:
            said.append(f"'{expr}' → 가장 최근 자료({last}년)까지")
        elif ref.get("start"):
            said.append(f"'{expr}' → {ref['start']}년부터 가장 최근 자료까지")
        else:
            said.append(f"'{expr}' → 가장 최근 자료까지")
    return ", ".join(said)


def user_text(
    question: str,
    parsed: dict[str, Any] | None,
    result: dict[str, Any] | None,
    today: date | None = None,
) -> str | None:
    """판정에 보내는 글. 요약할 답이 없으면 None"""
    summary = describe(result, parsed)
    if not summary:
        return None
    today = today or date.today()
    # '오늘' 줄에 자료가 어디까지 있는지를 고정 문구로 적지 않는다. 연도를 말하지 않은 질문은
    # 가장 최근 자료(주민등록인구는 올해 월별 값)로 답하므로(11/23 최신 자료 규칙), "연간 통계는
    # 2025년까지"라고 적으면 올해 월별 값으로 맞게 답한 것을 "기간이 다름"으로 막았다. 대신 답이
    # 실제로 쓴 자료 시점을 답의 안내 문장(notes)에서 그대로 옮긴다
    lines = [
        f"오늘: {today.isoformat()} (올해 {today.year}년)",
        f"질문: {question}",
    ]
    if read := time_read(parsed, result):
        lines.append(f"질문에서 읽은 시점: {read}")
    if used := data_used(result):
        lines.append(f"실제로 쓴 자료 시점: {used}")
    lines += ["결과: 답함", summary]
    return "\n".join(lines)


# 답의 안내 문장 중 자료 시점을 밝히는 것 (qa._add_latest_note · _add_data_end_note 가 붙인다)
DATA_TIME_NOTE = ("가장 최근", "자료가 ", "최신 달", "연간 자료")


def data_used(result: dict[str, Any] | None) -> str:
    """답이 실제로 쓴 자료 시점 한 줄 ("가장 최근 자료인 2026년 10월 기준으로 답했습니다").
    판정은 자료가 어디까지 있는지 모르므로 이 줄을 그대로 믿는다. 없으면 빈 문자열"""
    seen: list[str] = []
    for p in leaves(result or {}):
        for n in p.get("notes") or []:
            n = str(n)
            if any(k in n for k in DATA_TIME_NOTE) and n not in seen:
                seen.append(n)
    return " / ".join(seen)


@llm_calls.staged("answer_judge")
async def _ask(text: str) -> dict[str, Any] | None:
    result = await search_llm.GemmaClient(retry_waits=()).chat(
        MODEL,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
        thinking={"effort": "none"},
        maxCompletionTokens=MAX_TOKENS,
        temperature=0,
        responseFormat={"type": "json", "schema": SCHEMA},
    )
    return llm_json.loads(result.content)[0]


async def verdict(
    question: str, parsed: dict[str, Any] | None, result: dict[str, Any] | None
) -> dict[str, Any] | None:
    """이 답의 판정 {"reason", "missing", "verdict", "ms"}. 판정하지 못하면 None (답은 그대로)"""
    text = user_text(question, parsed, result)
    if text is None:
        return None
    start = time.perf_counter()
    try:
        out = await _ask(text)
    except Exception as e:
        logger.warning("답 판정 실패 → 답 그대로: %s: %s", type(e).__name__, e)
        return None
    if not out or out.get("verdict") not in VERDICTS:
        return None
    return {
        "reason": out.get("reason") or "없음",
        "missing": str(out.get("missing") or "")[:200],
        "verdict": out["verdict"],
        "ms": round((time.perf_counter() - start) * 1000),
    }
