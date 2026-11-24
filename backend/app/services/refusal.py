"""답변 불가 사유: 질문에 수치로 답하지 못했을 때 '왜 못 했는지'를 한곳에서 정한다

사유(응답의 status)는 답변 불가 유형 정의서의 코드를 쓴다.

  자료가 없음
    out_of_range        없는 연도 (아직 공표 전, 통계 시작 전)
    region_not_in_year  그해에 없던 지역 (출범 · 설치 · 편입 전)
    no_region_level     없는 지역 단위 (그 통계가 읍면동 · 시군구로 집계되지 않음)
    band_mismatch       맞는 구간 없음 (연령 · 체중 · 기간 · 임신 주수)
    missing_data        값이 비어 있음 (칸은 있지만 결측 기호 X · -)
    no_joint_table      함께 집계한 통계 없음 (한 집단이 두 조건을 함께 가진 수)
    partial_match       조건을 모두 만족하는 표 없음 (표는 찾았지만 조건 일부를 담지 않음)
  범위 밖 질문
    out_of_scope        범위 밖 주제 (인구 분야가 아닌 통계)
    causal_question · prediction_question   원인 · 예측 · 평가 질문
    not_statistics · chat                   통계 질문이 아님

판정은 각 단계가 한다 (지표 사전 · 조회 · 바로 계산 · 통계표 찾기). 이 모듈은 두 가지만 맡는다.
  early(question)     LLM 을 부르기 전에 말투만으로 알 수 있는 '통계 질문이 아님'
  finalize(res, q)    모든 단계를 거친 뒤의 답(res)을 보고 최종 사유를 고친다. 앞 단계는 "이
                      단계에서는 못 함"만 알기 때문에 끝에서 봐야 맞는 것들이다
                        - 읍면동을 물었다                 unknown_region → no_region_level
                        - 검토가 맞는 표 없다고 함          out_of_scope → 사유 분류 (LLM 1회)
                        - 표는 찾았는데 조건 일부가 ✘      tables → partial_match · band_mismatch
"""

import asyncio
import logging
import re
import time
from typing import Any

from app.prompts import refusal_reason as prompt
from app.services import flow, indicators, llm_calls, llm_json, search_llm
from app.services.josa import josa

logger = logging.getLogger(__name__)

MODEL = "HCX-007"  # GemmaClient 는 GEMMA_MODEL 로 바꿔 보낸다
MAX_TOKENS = 200

NAMES = {
    "out_of_range": "없는 연도",
    "region_not_in_year": "그해에 없던 지역",
    "no_region_level": "없는 지역 단위",
    "band_mismatch": "맞는 구간 없음",
    "missing_data": "값이 비어 있음",
    "no_joint_table": "함께 집계한 통계 없음",
    "partial_match": "조건을 모두 만족하는 표 없음",
    "out_of_scope": "범위 밖 주제",
    "causal_question": "원인·예측·평가 질문",
    "prediction_question": "원인·예측·평가 질문",
    "not_statistics": "통계 질문이 아님",
    "chat": "통계 질문이 아님",
}

EXAMPLES = "예) 2025년 서울 출생아 수 / 최근 5년 부산 인구 추이 / 합계출산율이 가장 높은 시도"

# ---- LLM 을 부르기 전에 알 수 있는 것 -------------------------------------------------------

# 수치를 묻는 말. 이 말이 있으면 통계 질문일 수 있어 여기서 거르지 않는다
ASKS_NUMBER = re.compile(
    r"몇|얼마|건수|비율|비중|순위|추이|통계|많아|적어|늘었|줄었|증가|감소|평균|인구\s*(?:는|가|수)"
)
# 행정 절차를 묻는 말 ("○○신고 어떻게 해?", "○○ 어디서 발급받아?")
PROCEDURE = re.compile(
    r"(?:신고|신청|발급|접수|등록|제출|이전|변경)\S*\s*(?:은|는|를|을)?\s*"
    r"(?:어떻게|어디서|어디에|어디로|방법|절차|하는\s*법|하려면|언제까지)"
    r"|(?:어떻게|어디서|어디에)\s*(?:신고|신청|발급|접수|등록|제출)"
    r"|필요한\s*서류|구비\s*서류"
)
# 추천 · 의견을 구하는 말. "어때?"는 살기 · 이사처럼 의견을 묻는 말과 함께일 때만 본다
# ("부산은 어때?"는 조건을 바꾸는 말이다). 이사 · 이주 · 정착은 하려는 일로 말할 때만 본다
# ("이주배경인구"는 통계 이름이다)
OPINION = (
    r"살기|살\s*만|살면|키우기|좋은|좋을|괜찮은"
    r"|이사\s*(?:가|갈|하|할|해)|(?:이주|정착)\s*(?:하|할|해)"
)
RECOMMEND = re.compile(
    rf"추천|골라\s*줘|어디가\s*(?:\S{{1,4}}\s*)?(?:좋|나을|낫)|괜찮을까|(?:{OPINION}).*어때"
)
# 챗봇이 무엇을 하는지, 어떻게 쓰는지 묻는 말
CAPABILITY = re.compile(
    r"(?:뭘|무엇을|뭐를?|무슨\s*일을?|어떤\s*(?:걸|것을|일을))\s*(?:할\s*수|해\s*줄\s*수|도와|물어)"
    r"|(?:사용|이용)\s*(?:법|방법)|어떻게\s*(?:쓰|써|사용|이용)"
    r"|(?:너|넌|너는|당신|챗봇)\S*\s*(?:누구|뭐야|뭐니|뭐예요|정체)"
)
GUIDES = {
    "procedure": (
        "행정 절차는 통계로 답할 수 없습니다. 신고 · 발급 방법은 정부24나 주민센터 같은 행정 "
        "서비스에서 확인해 주세요. 인구 통계는 이렇게 질문할 수 있습니다.\n" + EXAMPLES
    ),
    "recommend": (
        "추천이나 의견은 통계로 답할 수 없습니다. 대신 판단에 참고할 인구 통계는 볼 수 있습니다.\n"
        "예) 시군구별 합계출산율 / 영유아 인구가 많은 시군구 / 최근 5년 인구가 늘어난 시도"
    ),
    "capability": (
        "지역별 인구 통계를 찾아 숫자로 답하는 챗봇입니다. 인구 · 가구 · 출생 · 사망 · 혼인 · "
        "이혼 · 인구이동 · 외국인 통계를 볼 수 있습니다. 지역과 연도를 넣어 질문해 주세요.\n"
        + EXAMPLES
    ),
}


def early(question: str) -> tuple[str, str] | None:
    """질문 이해(HCX) 전에 말투만으로 알 수 있는 '통계 질문이 아님' → (status, 답 문장).
    수치를 묻는 말이 함께 있으면 거르지 않는다 ("출생신고 건수 몇 건이야"는 통계 질문)"""
    if ASKS_NUMBER.search(question):
        return None
    if CAPABILITY.search(question):
        return "chat", GUIDES["capability"]
    if PROCEDURE.search(question):
        return "not_statistics", GUIDES["procedure"]
    if RECOMMEND.search(question):
        return "not_statistics", GUIDES["recommend"]
    return None


def early_answer(question: str) -> dict[str, Any] | None:
    """early 의 /ask 응답 모양. 아니면 None"""
    hit = early(question)
    if hit is None:
        return None
    status, text = hit
    return {
        "status": status,
        "answer": text,
        "parsed": {},
        "result": {"status": status, "reason": text.split("\n")[0]},
        "sources": [],
        "chart": None,
        "trace": {},
    }


# ---- 끝에서 최종 사유 고치기 ----------------------------------------------------------------

SUB_REGION = re.compile(r"[가-힣0-9]{1,6}(?:동|읍|면|리)$")
# 지역 이름을 뺀 나머지가 이것뿐이면 지역 이름이다
REGION_REST = re.compile(r"[\s,·]*(?:광역시|특별시|특별자치시|특별자치도|지역)?[\s,·]*")
AGE_ITEM = re.compile(r"\d+\s*(?:살|세)|\d+\s*대")
REVIEW_NONE = "검토 결과 후보 통계표가 질문의 주제를 다루지 않습니다"
# 사유마다 정해 둔 문장만 내보낸다. 분류기가 쓴 설명(detail)은 사실과 다를 수 있어 기록(trace)에만
# 남기고 사용자에게 보이지 않는다 (11/23 세진)
TEMPLATES = {
    "no_region_level": (
        "이 통계는 질문한 지역 단위로는 집계되지 않습니다. "
        "더 넓은 지역 단위(시군구 · 시도)로 다시 질문해 주세요."
    ),
    "no_joint_table": (
        "두 조건을 함께 가진 대상을 따로 집계한 통계표가 없어 그 수를 알 수 없습니다. "
        "조건을 하나씩 나눠 질문하면 각각의 통계는 확인할 수 있습니다."
    ),
    "partial_match": (
        "주제에 맞는 통계표는 있지만 질문의 조건 일부를 담고 있지 않아 그 값을 낼 수 없습니다. "
        "그 조건을 빼거나 표에 있는 구분으로 다시 질문해 주세요."
    ),
    "not_statistics": "통계 수치를 묻는 질문이 아니라 답할 수 없습니다.\n" + EXAMPLES,
}


def _set(res: dict, status: str, answer: str | None, why: str) -> None:
    """최종 사유를 바꾼다. 바꾸기 전 status 는 trace 에 남긴다"""
    trace = res.setdefault("trace", {})
    trace["refusal"] = {"from": res.get("status"), "to": status, "why": why}
    res["status"] = status
    if answer:
        res["answer"] = answer
    result = res.setdefault("result", {})
    if isinstance(result, dict):
        result["refusal"] = status
    flow.add("답변 불가 사유", status, f"{NAMES.get(status, status)} · {why}")


def _failed(checks: dict) -> list[str]:
    return [k for k, v in (checks or {}).items() if v is False]


def band_only(checks: dict) -> list[str] | None:
    """표가 연령으로 나뉘어 있는데(class:연령 ✔) 어긋난 조건이 질문의 연령 구간뿐이면 그 구간들.
    아니면 None. 표의 구간(5세 단위)과 질문의 구간(62~67세)이 맞지 않는 경우다"""
    failed = _failed(checks)
    if not failed or (checks or {}).get("class:연령") is not True:
        return None
    if not all(k.startswith("item:") and AGE_ITEM.search(k) for k in failed):
        return None
    return [k.split(":", 1)[1] for k in failed]


def _band_text(rec: dict, bands: list[str]) -> str:
    asked = bands[0] if len(bands) == 1 else f"{bands[0]}~{bands[-1]}"
    return (
        f"'{rec.get('tbl_nm')}' 표의 연령 구간이 질문한 구간({asked})과 맞지 않아 그 값을 낼 수 "
        "없습니다. 표에 있는 연령 구간으로 다시 질문하면 확인할 수 있습니다."
    )


@llm_calls.staged("refusal_reason")
async def _ask(question: str, reason: str) -> dict[str, Any] | None:
    # Gemma가 응답하지 않으면 HCX로 넘어간다
    def request(llm: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        messages = [
            {"role": "system", "content": prompt.SYSTEM},
            {"role": "user", "content": prompt.user_text(question, reason)},
        ]
        return messages, {
            "thinking": {"effort": "none"},
            "maxCompletionTokens": MAX_TOKENS,
            # 다른 통계표 찾기 호출과 같게 Gemma만 0 (HCX는 서버 기본값)
            **({"temperature": 0} if llm == "gemma" else {}),
            "responseFormat": {"type": "json", "schema": prompt.SCHEMA},
        }

    result, _ = await search_llm.ask(MODEL, request)
    return llm_json.loads(result.content)[0]


async def classify(question: str, reason: str) -> dict[str, Any] | None:
    """검토가 적은 거절 이유 → {"code", "detail", "ms"}. 고르지 못하면 None (범위 밖 그대로)"""
    start = time.perf_counter()
    try:
        out = await _ask(question, reason)
    except Exception as e:
        logger.warning("거절 사유 분류 실패 → 범위 밖 그대로: %s: %s", type(e).__name__, e)
        return None
    if not out or out.get("code") not in prompt.CODES:
        return None
    return {
        "code": out["code"],
        "detail": str(out.get("detail") or "").strip()[:120],
        "ms": round((time.perf_counter() - start) * 1000),
    }


def _is_region(text: str) -> bool:
    """지역 이름뿐인 말 ("부산", "서울특별시", "서울 강남구"). 지역 이름 목록을 읽지 못하면
    지역으로 보지 않는다"""
    try:
        names = indicators.find_regions(text)
    except Exception as e:  # 연결 실패 · 테이블 없음: 거절 문장을 고르다 답 전체를 잃지 않는다
        logger.warning("지역 이름 확인 실패 → 범위 밖 지표 그대로: %s: %s", type(e).__name__, e)
        return False
    rest = re.sub(r"\s+", "", text)  # find_regions는 띄어쓰기를 지운 이름을 준다
    for name in sorted(names, key=len, reverse=True):
        rest = rest.replace(name, " ")
    return bool(names) and REGION_REST.fullmatch(rest) is not None


def outside_indicators(res: dict[str, Any]) -> tuple[list[str], list[str]]:
    """통계표 찾기의 계획이 '인구 분야 밖'으로 적은 지표들과, 볼 수 있는 나머지 지표들.
    계획이 지역 이름을 지표로 적은 것("부산은?"의 '부산')은 뺀다. 지역 이름 목록을 DB에서
    읽으므로 스레드에서 부른다"""
    tables = res.get("tables") or {}
    handoff = tables.get("handoff") or {}
    outside = (handoff.get("combine") or {}).get("out_of_scope") or (tables.get("multi") or {}).get(
        "out_of_scope"
    )
    rest = [t.get("span") for t in handoff.get("targets") or [] if t.get("span")]
    return [str(o) for o in outside or [] if not _is_region(str(o))], rest


def _outside_text(outside: list[str], rest: list[str]) -> str:
    names = " · ".join(f"'{o}'" for o in outside)
    # 받침에 맞춘 조사 ("'미세먼지'는", "'집값'은")
    subject = josa(names, "은는") if len(outside) == 1 else f"{names} 등은"
    text = (
        f"{subject} 인구 분야 통계가 아니라 제공하지 "
        "않습니다. KOSIS의 해당 분야 통계에서 찾을 수 있습니다. 이 조건을 빼면 질문이 달라져 "
        "숫자를 내지 않았습니다."
    )
    if rest:
        text += f" 대신 {josa(' · '.join(rest), '은는')} 따로 질문하면 확인할 수 있습니다."
    return text


async def finalize(res: dict[str, Any], question: str) -> None:
    """모든 단계를 거친 답(res)의 최종 사유를 고친다. 되물었으면 그대로.
    답한 경우는 한 가지만 본다: 물은 지표 중 하나가 인구 분야 밖이면 나머지로 낸 답도 거절한다"""
    status = res.get("status")
    if res.get("clarification") or (res.get("tables") or {}).get("clarify"):
        return
    outside, rest = await asyncio.to_thread(outside_indicators, res)
    if outside and status in ("ok", "tables"):
        # 지표 여럿 중 하나가 범위 밖 (11/23 세진): 그 지표를 빼고 답하면 다른 질문의 답이 된다.
        # 전체를 범위 밖으로 거절하고 나머지 지표는 볼 수 있다고 안내한다
        _set(res, "out_of_scope", _outside_text(outside, rest), f"범위 밖 지표: {outside}")
        return
    if status == "ok":
        return
    result = res.get("result") if isinstance(res.get("result"), dict) else {}
    tables = res.get("tables") or {}
    recs = tables.get("recommendations") or []
    top = recs[0] if recs else {}

    if status == "unknown_region" and SUB_REGION.search(str(result.get("region") or "")):
        # 읍면동을 물었다: 지역을 모르는 것이 아니라 그 단위로 집계한 자료가 없다
        name = result.get("region")
        where = f" 속한 시군구({result['alternative']})로" if result.get("alternative") else ""
        text = (
            f"'{name}' 같은 읍면동 단위로는 이 통계가 집계되지 않습니다."
            f"{where or ' 속한 시군구나 시도 이름으로'} 다시 질문해 주세요."
        )
        _set(res, "no_region_level", text, "읍면동 단위를 물음")
        return

    reason = str(tables.get("reason") or "")
    if status == "out_of_scope" and reason.startswith(REVIEW_NONE):
        # 검토는 주제가 다를 때만이 아니라 지역 단위 · 세부 분류가 없을 때도 '맞는 표 없음'으로
        # 끝낸다. 검토가 적은 이유를 읽어 사유를 고른다
        picked = await classify(question, reason[len(REVIEW_NONE) :].strip(" ()"))
        if picked is None or picked["code"] == "out_of_scope":
            return
        res.setdefault("trace", {})["refusal_reason"] = picked
        why = f"검토의 거절 이유를 분류 ({picked['detail']})"
        _set(res, picked["code"], TEMPLATES[picked["code"]], why)
        return

    if status == "tables" and tables.get("status") == "partial_match" and not tables.get("clarify"):
        # 표는 찾았지만 조건 일부(✘)를 담지 않아 수치를 내지 못했다
        if bands := band_only(top.get("checks") or {}):
            text = f"{_band_text(top, bands)}\n{res.get('answer') or ''}"
            _set(res, "band_mismatch", text, "✘가 연령 구간뿐")
        else:
            _set(res, "partial_match", None, tables.get("notice") or "조건 일부 ✘")
        return

    if status in ("no_region_level", "missing_data") and top and not result.get("suppressed"):
        # 사전의 표는 그 지역 단위가 없어 끝났지만, 다른 표를 찾아보니 지역 단위는 맞고 연령
        # 구간만 맞지 않는다: 막힌 이유는 지역 단위가 아니라 구간이다
        checks = top.get("checks") or {}
        if checks.get("region_level") is True and (bands := band_only(checks)):
            why = "다른 표는 지역 단위 ✔, 연령 구간 ✘"
            _set(res, "band_mismatch", _band_text(top, bands), why)
