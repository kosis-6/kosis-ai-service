import json
import logging
import re
import time
from typing import Any

from app.core.config import get_settings
from app.prompts.llm_pick import SYSTEM, SYSTEM_GEMMA, TOGETHER_SYSTEM
from app.services import llm_calls, search_llm


def se_norm(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


logger = logging.getLogger(__name__)

MODEL = "HCX-007"
TOP_K = 8
OFF_DOMAIN = re.compile(
    r"교통사고|암\s*환자|코로나|질병|환자|관리비|난방비|등록금|월급|임금|소득|집값|매매가|전세가|분양가|"
    r"범죄|자살|목숨을 끊|"
    r"흡연|담배|음주|의사가|의사 수|병원|병상|빈집|학업중단|진학률|"
    r"(?<!등)(?<!중)(?<!대)(?<!원)(?<!재)학생\s*수|(?<!등)(?<!중)(?<!대)(?<!원)(?<!재)학생이 몇|"
    r"학급|한 반에"
)


CAND_VALUES = 12
ANSWERED_BY = "_llm"


def class_values_text(c: dict[str, Any]) -> str:
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
    llm = answered or search_llm.provider()
    return get_settings().llm_pick_mode or ("pick_decided" if llm == "gemma" else "filter")


@llm_calls.staged("llm_pick")
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
        parsed[ANSWERED_BY] = llm
    return parsed


WORD_CHECKS = ("item", "class", "target")


def trust_exact(out: dict[str, Any]) -> dict[str, Any]:
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


TOGETHER_MAX = 2
SIBLING_NAME_SIM = 0.75


def siblings(picked: dict[str, Any], cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.services import search as se

    return [
        c
        for c in cands
        if c["family_id"] != picked["family_id"]
        and c["survey_name"] == picked["survey_name"]
        and se.name_similarity(picked["name_n"], c["name_n"]) >= SIBLING_NAME_SIM
        and picked["name_n"] not in c["name_n"]
        and c["name_n"] not in picked["name_n"]
    ]


@llm_calls.staged("together")
async def _ask_together(
    question: str, picked: dict[str, Any], others: list[dict[str, Any]]
) -> list[str]:
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
    except Exception as e:
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
    from app.services import search as se

    mode = pick_mode(answered)
    undecided = result["status"] == "clarify"
    if mode == "pick_decided":
        mode = "filter" if undecided else "pick"
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
    if undecided:
        from app.services import synonyms

        said = se.norm(synonyms.keyword_text(f"{question} {slots.get('target') or ''}"))
        target = picked.get("target") or "전체"
        if target != "전체" and se.norm(target) not in said:
            return {**result, "llm_pick": {**trace["llm_pick"], "ignored": "질문에 없는 대상"}}
        slots["tbl_id"] = pick
    redone = se.resolve(tables, slots, picked["family_id"], allow_clarify=not undecided)
    redone["candidates"] = result.get("candidates", [])
    redone["other_families"] = result.get("other_families", [])
    redone["llm_pick"] = trace["llm_pick"]
    return redone
