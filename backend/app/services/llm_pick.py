"""검색 뒤 HCX-007 최종 검토 (실전 2, 실험실 §56-4·§61-3): 상위 후보(TOP_K개) 중 질문에 맞는 표 하나를
고르고,
어느 후보도 질문 주제를 다루지 않으면 범위 밖으로 돌린다.

- HCX-007, 생각 끄기(thinking none), Structured Outputs(responseFormat json + 후보 tbl_id enum)라
  JSON이 깨지지 않고
  약 1.5초·900토큰. 실험실: 범위 밖 재현율 30/30, 거짓 거절 1/102, 새 테스트셋 흐름 68→74.
- 고른 표의 패밀리가 1위와 다르면 그 패밀리로 멤버·판 선택을 다시 한다(resolve). 되묻기(clarify)가
  필요한 결과는
  건드리지 않는다. 호출이 실패하면 검색 결과를 그대로 둔다.
- 모드(LLM_PICK_MODE): filter(기본) = 거르기만 / pick = 고른 표로 패밀리 교체 /
  resolve = 확정 결과는 거르기만,
  멤버 되묻기(clarify) 상태에서는 고른 표로 멤버·판까지 확정 (되묻기 횟수가 준다). 패밀리 모호는 1위
  유지.
- 설정 LLM_PICK=false 로 끈다.
"""

import json
import logging
import re
import time
from typing import Any

from app.core.config import get_settings
from app.prompts.llm_pick import SYSTEM, SYSTEM_GEMMA, TOGETHER_SYSTEM
from app.services import search_llm


def se_norm(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


logger = logging.getLogger(__name__)

MODEL = "HCX-007"
# 후보 수 5 → 8: 정답이 6~8위인 문항(d21 전출지·전입지, d54 시도별 외국인과의 혼인, t34 외국인
# 근로자,
# t26 출산순위별 출생)을 LLM 이 보지 못했다 (§73)
TOP_K = 8
# 인구 통계 카탈로그에 없는 주제. 질문에 이 말이 있는데 어느 후보의 이름·분류·항목에도 없으면 LLM 을
# 거치지 않고 범위 밖
# (레포 측정 §68-6: LLM 이 '교통사고 사망'→사망자수, '암 환자'→사망자수, '의사 수'→직업별 취업자처럼
# 주제 인접 표를 partial 로 통과시킴)
OFF_DOMAIN = re.compile(
    r"교통사고|암\s*환자|코로나|질병|환자|관리비|난방비|등록금|월급|임금|소득|집값|매매가|전세가|분양가|"
    r"범죄|자살|목숨을 끊|"
    r"흡연|담배|음주|의사가|의사 수|병원|병상|빈집|학업중단|진학률|"
    # '학생 수'는 학교 통계지만 '대학생 · 고등학생 수'는 인구총조사(교육상태 · 연령)로 답한다
    r"(?<!등)(?<!중)(?<!대)(?<!원)(?<!재)학생\s*수|(?<!등)(?<!중)(?<!대)(?<!원)(?<!재)학생이 몇|"
    r"학급|한 반에"
)


CAND_VALUES = 12  # 후보 설명에 보여 줄 분류 값 개수 (넘으면 '외 N개')
ANSWERED_BY = "_llm"  # _ask 가 검토 결과에 붙이는 칸: 답한 LLM ("hcx" · "gemma")


def class_values_text(c: dict[str, Any]) -> str:
    """분류 값 요약. 하위 단계 값('청각' 등)도 보이도록 전체 값(values)을 우선 쓴다"""
    values = c.get("values") or c.get("sample_values") or []
    text = ", ".join(values[:CAND_VALUES])
    if len(values) > CAND_VALUES:
        text += f" 외 {len(values) - CAND_VALUES}개"
    return text


def cand_text(t: dict[str, Any], i: int) -> str:
    cls = "; ".join(
        f"{c['name']}({class_values_text(c)})" for c in (t["classifications"] or [])[:4]
    )
    items = ", ".join(x["name"] for x in (t["items"] or [])[:8])
    return (
        f"[{i}] tbl_id={t['tbl_id']} | {t['table_name']} | 조사: {t['survey_name']} | "
        f"수록: {t['period']} | "
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


def pick_mode(answered: str | None = None) -> str:
    """검토 모드. LLM_PICK_MODE 를 비워 두면 검토에 답한 LLM 에 맞춘다
    (11/15 까지 잰 조합: HCX → filter, Gemma → pick_decided).
    answered: 이번 검토에 실제로 답한 LLM. Gemma 가 응답하지 않아 HCX 가 답했으면 "hcx" 다"""
    llm = answered or search_llm.provider()
    return get_settings().llm_pick_mode or ("pick_decided" if llm == "gemma" else "filter")


async def _ask(question: str, cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    user = f"질문: {question}\n\n후보:\n" + "\n".join(
        cand_text(t, i + 1) for i, t in enumerate(cands)
    )
    ids = [t["tbl_id"] for t in cands]

    def request(llm: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        gemma = llm == "gemma"
        messages = [
            {"role": "system", "content": SYSTEM_GEMMA if gemma else SYSTEM},
            {"role": "user", "content": user},
        ]
        return messages, {
            "thinking": {"effort": "none"},
            "maxCompletionTokens": 300,
            # Gemma 는 고른 표로 실제로 바꾸므로(pick_decided) 같은 질문에 같은 표가 나오게
            # 0 으로 둔다. HCX 는 지금까지처럼 서버 기본값
            **({"temperature": 0} if gemma else {}),
            "responseFormat": {
                "type": "json",
                "schema": {
                    "type": "object",
                    "properties": {
                        "pick": {"type": "string", "enum": [*ids, "none"]},
                        "match": {"type": "string", "enum": ["exact", "partial", "none"]},
                        "item": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["pick", "match", "item", "reason"],
                },
            },
        }

    result, llm = await search_llm.ask(MODEL, request)
    parsed = parse(result.content)
    if parsed is not None:
        parsed[ANSWERED_BY] = llm  # review 가 꺼내 검토 모드를 정한다
    return parsed


WORD_CHECKS = ("item", "class")  # 낱말이 표에 있는지로 판정하는 조건


def trust_exact(out: dict[str, Any]) -> dict[str, Any]:
    """검토가 '모두 충족(exact)'으로 고른 표에는 코드의 낱말 ✘(항목 · 분류)를 붙이지 않는다.

    코드는 낱말이 표에 글자로 있는지를 보고, 검토는 뜻으로 판단한다. '식당이나 술집에서 일하는'에
    검토가 '음식점 및 주점업'이 든 표를 맞게 골라도 코드가 '술집 ✘'를 붙여 '일부 일치'로 나갔다.
    연도 · 지역 단위 · 성별처럼 낱말 차이가 아닌 조건과 판 메모가 있으면 그대로 둔다
    (11/16 Gemma 668문항: 정답 수 그대로, '일부 일치' 72 → 35, 그중 정답 표였던 것 64 → 29)"""
    if out.get("status") != "partial_match" or out.get("notes"):
        return out
    pick = out.get("llm_pick") or {}
    table = out.get("table") or {}
    if (pick.get("raw") or {}).get("match") != "exact" or pick.get("pick") != table.get("tbl_id"):
        return out
    failed = out.get("failed") or []
    if not failed or any(k.split(":")[0] not in WORD_CHECKS for k in failed):
        return out
    checks = {k: v for k, v in (table.get("checks") or {}).items() if k not in failed}
    return {
        **out,
        "status": "ok",
        "reason": None,
        "failed": [],
        "llm_confirmed": failed,
        "table": {**table, "checks": checks},
    }


def review_candidates(
    result: dict[str, Any], by: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """검토에 넘기는 후보: 패밀리당 하나(점수 최고 표)만, 최대 TOP_K 개.
    표 단위로 주면 같은 표의 연도판·지역판이 자리를 다 차지해('노인 생활비' 후보 10개 중 9개가
    생활비원천 표의 판) LLM 이 다른 주제의 표를 보지 못했다 (§79)"""
    cands: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in result.get("candidates") or []:
        t = by.get(c["tbl_id"])
        if t is None or t["family_id"] in seen:
            continue
        seen.add(t["family_id"])
        cands.append(t)
        if len(cands) >= TOP_K:
            break
    return cands


# ---- 함께 볼 표: 한 통계가 여러 표로 나뉜 경우 (외국인 남편 표 / 외국인 아내 표) ----
TOGETHER_MAX = 2
# 표 이름 글자 겹침. 남편 / 아내만 다른 표가 0.80, 분류 축이 하나 더 붙은 표가 0.73
SIBLING_NAME_SIM = 0.75


def siblings(picked: dict[str, Any], cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """고른 표와 같은 조사이고 표 이름이 거의 같은 후보 (한두 낱말만 다른 표).
    한 표 이름이 다른 표 이름을 통째로 담으면 나뉜 표가 아니라 그 일부만 따로 낸 표라 뺀다
    ('시도/임신기간별 출생'과 '시도/임신기간별 출생(쌍태아 이상)')"""
    from app.services import search as se  # 순환 참조를 피해 여기서 읽는다

    return [
        c
        for c in cands
        if c["family_id"] != picked["family_id"]
        and c["survey_name"] == picked["survey_name"]
        and se.name_similarity(picked["name_n"], c["name_n"]) >= SIBLING_NAME_SIM
        and picked["name_n"] not in c["name_n"]
        and c["name_n"] not in picked["name_n"]
    ]


async def _ask_together(
    question: str, picked: dict[str, Any], others: list[dict[str, Any]]
) -> list[str]:
    """고른 표와 이름이 거의 같은 후보를 주고, 함께 봐야 하는 표의 tbl_id 를 받는다.
    Gemma 에만 묻는다 (HCX-007 은 같은 요청 10개 모두에 표를 덧붙였다, 11/17). 실패하면 올린다"""
    user = (
        f"질문: {question}\n\n고른 표:\n"
        + cand_text(picked, 0).replace("[0]", "[고른 표]", 1)
        + "\n\n다른 후보:\n"
        + "\n".join(cand_text(t, i + 1) for i, t in enumerate(others))
    )
    ids = [t["tbl_id"] for t in others]
    schema = {
        "type": "object",
        "required": ["also"],
        "properties": {
            "also": {
                "type": "array",
                "maxItems": TOGETHER_MAX,
                "items": {
                    "type": "object",
                    "required": ["tbl_id", "kind", "note"],
                    "properties": {
                        "tbl_id": {"type": "string", "enum": ids},
                        "kind": {"type": "string", "enum": ["together"]},
                        "note": {"type": "string"},
                    },
                },
            }
        },
    }
    messages = [{"role": "system", "content": TOGETHER_SYSTEM}, {"role": "user", "content": user}]
    result = await search_llm.GemmaClient(retry_waits=()).chat(
        MODEL,
        messages,
        thinking={"effort": "none"},
        maxCompletionTokens=250,
        temperature=0,
        responseFormat={"type": "json", "schema": schema},
    )
    also = (parse(result.content) or {}).get("also") or []
    picked_ids = [a.get("tbl_id") for a in also if isinstance(a, dict)]
    return [i for i in dict.fromkeys(picked_ids) if i in ids][:TOGETHER_MAX]


async def with_together(
    question: str, out: dict[str, Any], tables: list[dict[str, Any]]
) -> dict[str, Any]:
    """표를 고른 뒤, 한 통계가 여러 표로 나뉘어 있으면 함께 볼 표를 덧붙인다 (out["together"]).

    고르는 표는 바꾸지 않는다. Gemma 가 검토에 답했고, 고른 표와 이름이 거의 같은 표가 후보에
    있을 때만 묻는다 (11/17 668문항: 호출 32%, 고른 표가 달라진 문항 0, 덧붙은 문항 5).
    덧붙이기가 실패해도 답은 그대로 나간다"""
    pick = out.get("llm_pick") or {}
    tbl_id = (out.get("table") or {}).get("tbl_id")
    if (
        not get_settings().llm_pick_together
        or out.get("status") not in ("ok", "partial_match")
        or pick.get("llm") != "gemma"
        or not tbl_id
    ):
        return out
    by = {t["tbl_id"]: t for t in tables}
    if tbl_id not in by:
        return out
    near = siblings(by[tbl_id], review_candidates(out, by))
    if not near:
        return out
    start = time.perf_counter()
    try:
        together = await _ask_together(question, by[tbl_id], near)
    except Exception as e:
        logger.warning("함께 볼 표 찾기 실패 → 고른 표만 안내: %s: %s", type(e).__name__, e)
        pick["together"] = {"error": f"{type(e).__name__}: {e}"[:200]}
        return out
    pick["together"] = {"asked": [t["tbl_id"] for t in near], "picked": together}
    pick["together"]["ms"] = round((time.perf_counter() - start) * 1000)
    return {**out, "together": together} if together else out


async def review(
    question: str, result: dict[str, Any], tables: list[dict[str, Any]], trace: dict[str, Any]
) -> dict[str, Any]:
    """검색 결과에 LLM 검토를 얹는다. 후보가 없거나 되묻기면 그대로"""
    out = trust_exact(await _review(question, result, tables, trace))
    return await with_together(question, out, tables)


async def _review(
    question: str, result: dict[str, Any], tables: list[dict[str, Any]], trace: dict[str, Any]
) -> dict[str, Any]:
    if not get_settings().llm_pick:
        return result
    if result.get("status") not in ("ok", "partial_match", "ambiguous", "clarify"):
        return result
    by = {t["tbl_id"]: t for t in tables}
    cands = review_candidates(result, by)
    cand_ids = [t["tbl_id"] for t in cands]
    if not cands:
        return result
    if (m := OFF_DOMAIN.search(question)) and not any(
        se_norm(m.group(0)) in (t["name_n"] + t["meta_n"] + t["path_n"]) for t in cands
    ):
        trace["llm_pick"] = {"off_domain": m.group(0)}
        return {
            **result,
            "status": "out_of_scope",
            "reason": f"'{m.group(0)}'은 인구 통계 범위 밖입니다",
            "llm_pick": trace["llm_pick"],
        }
    start = time.perf_counter()
    try:
        parsed = await _ask(question, cands)
    except Exception as e:  # 검토 실패는 검색 결과를 바꾸지 않는다
        logger.warning("LLM 검토 실패 → 검색 결과 그대로: %s: %s", type(e).__name__, e)
        trace["llm_pick"] = {"error": f"{type(e).__name__}: {e}"}
        return result
    answered = parsed.pop(ANSWERED_BY, None) if parsed else None
    pick = norm_pick(parsed, cand_ids)
    trace["llm_pick"] = {
        "raw": parsed,
        "pick": pick,
        "ms": round((time.perf_counter() - start) * 1000),
    }
    if answered:
        trace["llm_pick"]["llm"] = answered
    # 거절은 match=none 일 때만. partial(조건이 조금 다른 표)은 안내한다 (실험실·레포 측정: 옛
    # 판·넓은 표를
    # 거절해 생기던 거짓 거절 6건 대응). answerable(구 형식)도 받는다
    match = (parsed or {}).get("match")
    answerable = match != "none" if match else bool((parsed or {}).get("answerable", True))
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
    from app.services import search as se  # 순환 참조를 피해 여기서 읽는다

    mode = pick_mode(answered)
    # 멤버 되묻기(clarify)만 LLM 이 확정한다. 패밀리 모호(ambiguous)는 1위 패밀리를 두는 편이 나았음
    # (레포 측정 §68-4: LLM 이 같은 조사의 형제 패밀리로 바꿔 8건 손실, 얻은 건 1건)
    undecided = result["status"] == "clarify"
    if mode == "pick_decided":  # 멤버 되묻기 상태에서는 거르기만, 그 밖에는 고른 표로 바꾼다
        mode = "filter" if undecided else "pick"
    # '우리 구로 이사 오는'에 어느 시도인지 되묻는 결과는 어느 모드에서도 다른 표로 바꾸지 않는다
    # (11/15 실험실: pick 이 '구로'를 서울 구로구로 읽어 서울 표로 확정했다).
    # 범위 밖 거르기는 위에서 이미 했다
    if result["status"] == "ambiguous" and result.get("reason") == se.REGION_ASK:
        mode = "filter"
    if mode == "filter" or (mode == "resolve" and not undecided):
        return {**result, "llm_pick": trace["llm_pick"]}
    picked = by[pick]
    current = (result.get("family") or {}).get("family_id") or (result.get("table") or {}).get(
        "family_id"
    )
    if picked["family_id"] == current and not undecided:
        return {**result, "llm_pick": trace["llm_pick"]}
    slots = dict(result["slots"])
    if undecided:  # 되묻기 대신 LLM 이 고른 표로 멤버·판까지 확정
        from app.services import synonyms

        said = se.norm(synonyms.keyword_text(f"{question} {slots.get('target') or ''}"))
        target = picked.get("target") or "전체"
        if target != "전체" and se.norm(target) not in said:
            # 질문에 없는 대상('기타', '혼인 귀화자')을 LLM 이 골랐으면 기본값 규칙(전체·계)을
            # 지킨다
            return {**result, "llm_pick": {**trace["llm_pick"], "ignored": "질문에 없는 대상"}}
        slots["tbl_id"] = pick
    redone = se.resolve(tables, slots, picked["family_id"], allow_clarify=not undecided)
    redone["candidates"] = result.get("candidates", [])
    redone["other_families"] = result.get("other_families", [])
    redone["llm_pick"] = trace["llm_pick"]
    return redone
