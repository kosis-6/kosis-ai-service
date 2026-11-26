"""통합 보고서 생성: 비교 검사 → LLM 문장 → 검증 → 자리표시자 채우기 (명세서 5장)

LLM은 수치를 받지도 쓰지도 않는다. {r1.value} · {c1.value} 자리표시자만 쓰고, 코드가 검증한 뒤
값을 채운다. 검증에 걸리면 한 번 다시 쓰게 하고, 또 걸리거나 LLM을 부르지 못하면 각 결과의 요약
문장(코드가 만든 것)만으로 보고서를 만든다. 그래서 보고서의 모든 숫자는 담은 결과에서 온다.
"""

import json
import logging
import re
from typing import Any

from app.core.config import get_settings
from app.prompts import report as prompt
from app.services import llm_calls, llm_json, report_compare, search_llm
from app.services.hcx_client import HcxClient

logger = logging.getLogger(__name__)

MODEL = "HCX-007"
MAX_TOKENS = 1500
PLACEHOLDER = re.compile(r"\{([rc]\d+)\.value\}")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
CAUSAL = ("때문", "원인", "영향")
UP = re.compile(r"증가|늘|상승|높아|올라|커졌")
DOWN = re.compile(r"감소|줄|하락|낮아|떨어|작아졌")
# 담은 결과에 없는 추측 · 제언 · 근거 없는 정도 표현
SPECULATION = ("필요", "대비", "마련", "시사", "우려", "상당", "대부분", "급격", "크게", "심각")
# 비교 · 증감 표현: 코드가 계산한 비교(c*)를 근거로 달아야 쓸 수 있다
COMPARISON = re.compile(r"증가|감소|늘|줄|많|적|높|낮|상승|하락|보다|차이|커|작")
TREND = ("추세", "증가세", "감소세", "꾸준히")
STEADY = ("꾸준히", "계속", "지속", "줄곧", "매년")
PROJECTION = ("예상", "전망", "추계")
GUESS = ("예상", "전망", "추계", "것으로 보", "것으로 예측", "될 것")
SECTIONS = ("summary", "analysis")


def llm_input(title: str, purpose: str | None, items: list[dict], cmp: dict) -> dict[str, Any]:
    """LLM에 줄 것: 이름 · 조건 · 단위 · 방향. 값은 주지 않는다"""
    return {
        "title": title,
        "purpose": purpose,
        "items": [
            {
                "id": it["id"],
                "label": it["figure"].get("label"),
                "indicator": it["figure"].get("indicator"),
                "kind": it["figure"].get("kind"),
                "unit": it["figure"].get("unit"),
                "value": None if it["figure"].get("value") is None else f"{{{it['id']}.value}}",
                "conditions": it["figure"].get("conditions"),
                "projection": bool(it["figure"].get("is_projection")),
            }
            for it in items
        ],
        "derived": [
            {
                "id": d["id"],
                "of": d["of"],
                "meaning": d["meaning"],
                "unit": d["unit"],
                "direction": d["direction"],
                **({"steady": d["steady"]} if "steady" in d else {}),
            }
            for d in cmp["derived"]
        ],
        "not_comparable": [{"pair": x["pair"], "why": x["why"]} for x in cmp["not_comparable"]],
        "trend_ok": cmp["trend_ok"],
    }


def _allowed_numbers(title: str, purpose: str | None, items: list[dict], cmp: dict) -> set[str]:
    words = [title, purpose or ""]
    for it in items:
        f = it["figure"]
        words += [f.get("label") or "", f.get("indicator") or ""]
        words += [str(v) for v in (f.get("conditions") or {}).values() if v]
    words += [d["meaning"] for d in cmp["derived"]]
    return {n for w in words for n in NUMBER.findall(w)}


def check(
    out: dict | None, items: list[dict], cmp: dict, allowed: set[str]
) -> tuple[dict[str, list[dict]], list[str], list[dict]]:
    """(쓸 문장, 다시 쓰게 할 문제, 버린 문장)"""
    if not isinstance(out, dict):
        return {section: [] for section in SECTIONS}, ["JSON을 읽지 못했습니다."], []
    by_id = {it["id"]: it for it in items}
    valued = {i for i, it in by_id.items() if it["figure"].get("value") is not None}
    valued |= {d["id"] for d in cmp["derived"]}
    known = set(by_id) | {d["id"] for d in cmp["derived"]}
    projected = {i for i, it in by_id.items() if it["figure"].get("is_projection")}
    blocked = [set(x["pair"]) for x in cmp["not_comparable"]]
    steady = {d["id"] for d in cmp["derived"] if d.get("steady")}
    problems: list[str] = []
    dropped: list[dict] = []
    kept: dict[str, list[dict]] = {}

    def bad(text: str, why: str) -> None:
        problems.append(why)
        dropped.append({"text": text, "why": why})

    for section in SECTIONS:
        kept[section] = []
        for s in out.get(section) or []:
            if not isinstance(s, dict) or not isinstance(s.get("text"), str):
                continue
            text = " ".join(s["text"].split())
            used = PLACEHOLDER.findall(text)
            refs = {r for r in s.get("refs") or [] if isinstance(r, str)} | set(used)
            if unknown := [u for u in used if u not in valued]:
                bad(text, f"없는 자리표시자를 썼습니다: {', '.join(sorted(set(unknown)))}")
                continue
            if stray := [n for n in NUMBER.findall(PLACEHOLDER.sub("", text)) if n not in allowed]:
                bad(text, f"숫자를 직접 썼습니다 ({', '.join(stray)}). 자리표시자를 쓰세요.")
                continue
            if any(w in text for w in CAUSAL):
                bad(text, f"원인이나 영향을 썼습니다: {text}")
                continue
            if hit := [w for w in SPECULATION if w in text]:
                bad(text, f"결과에 없는 추측 · 제언 · 정도 표현({', '.join(hit)}): {text}")
                continue
            if COMPARISON.search(PLACEHOLDER.sub("", text)) and not any(
                r.startswith("c") for r in refs
            ):
                bad(text, f"비교 · 증감은 derived(c*)를 refs 에 넣을 때만 씁니다: {text}")
                continue
            if wrong := _wrong_direction(text, refs, cmp):
                bad(text, f"{wrong} 방향이 계산과 반대입니다: {text}")
                continue
            if not cmp["trend_ok"] and any(w in text for w in TREND):
                bad(text, f"trend_ok 가 false 인데 추세 표현을 썼습니다: {text}")
                continue
            if any(w in text for w in STEADY) and not refs & steady:
                bad(text, f"steady 인 derived 없이 '꾸준히/계속'을 썼습니다: {text}")
                continue
            if refs & projected and not any(w in text for w in PROJECTION):
                bad(text, f"추계 결과를 쓰면서 '예상/전망/추계'가 없습니다: {text}")
                continue
            if not refs & projected and any(w in text for w in GUESS):
                bad(text, f"실제 통계를 예상 · 전망처럼 썼습니다 (실측값은 '~였습니다'): {text}")
                continue
            if not refs or not refs <= known:
                dropped.append({"text": text, "why": "근거(refs)가 없거나 모르는 id"})
                continue
            if any(pair <= refs for pair in blocked):
                dropped.append({"text": text, "why": "비교할 수 없는 두 결과를 한 문장에 씀"})
                continue
            kept[section].append({"text": text, "refs": sorted(refs)})
    if not problems and not kept["summary"]:
        problems.append("summary 가 비었습니다.")
    if missing := unvalued(kept, items):
        problems.append(
            f"값이 있는 결과는 {{rN.value}}로 한 번씩은 쓰세요. 빠진 것: {', '.join(missing)}"
        )
    return kept, problems, dropped


def _wrong_direction(text: str, refs: set[str], cmp: dict) -> str | None:
    """근거로 단 변화(change · series_change)의 방향과 반대 말을 썼으면 그 id"""
    plain = PLACEHOLDER.sub("", text)
    up, down = bool(UP.search(plain)), bool(DOWN.search(plain))
    for d in cmp["derived"]:
        if d["id"] not in refs or d["kind"] not in ("change", "series_change"):
            continue
        if d["direction"] == "증가" and down and not up:
            return d["id"]
        if d["direction"] == "감소" and up and not down:
            return d["id"]
    return None


def unvalued(kept: dict[str, list[dict]], items: list[dict]) -> list[str]:
    """값이 있는데 어느 문장에도 그 값({rN.value})이 나오지 않은 결과"""
    shown = {u for sec in kept.values() for x in sec for u in PLACEHOLDER.findall(x["text"])}
    return [
        it["id"] for it in items if it["figure"].get("value") is not None and it["id"] not in shown
    ]


def fill(text: str, values: dict[str, str], units: dict[str, str] | None = None) -> str:
    """자리표시자를 값으로 바꾼다. 값에 단위가 들어 있으니 바로 뒤에 LLM이 다시 쓴 단위는 지운다"""
    for key, unit in (units or {}).items():
        if unit:
            pattern = r"(\{" + key + r"\.value\})\s*" + re.escape(unit)
            text = re.sub(pattern, r"\1", text)
    return PLACEHOLDER.sub(lambda m: values[m.group(1)], text)


@llm_calls.staged("report_write")
async def _ask(messages: list[dict]) -> dict | None:
    settings = get_settings()
    client = (
        search_llm.GemmaClient(retry_waits=()) if settings.report_llm == "gemma" else HcxClient()
    )
    result = await client.chat(
        MODEL,
        messages,
        thinking={"effort": "none"},
        maxCompletionTokens=MAX_TOKENS,
        temperature=0.2,
    )
    return llm_json.loads(result.content)[0]


def _fallback(items: list[dict]) -> dict[str, list[dict]]:
    """LLM 없이: 각 결과의 요약 첫 문장 (코드가 만든 문장이라 숫자가 결과 그대로다)"""
    lines = []
    for it in items:
        headline = ((it["snapshot"].get("summary") or {}).get("headline") or "").strip()
        if not headline:
            headline = f"{it['figure'].get('label')}: {report_compare.value_text(it)}"
        if it["figure"].get("is_projection") and not any(w in headline for w in PROJECTION):
            headline += " (장래추계 예상값)"
        lines.append({"text": headline, "refs": [it["id"]]})
    return {"summary": lines, "analysis": []}


async def write(title: str, purpose: str | None, snapshots: list[dict]) -> dict[str, Any]:
    """보고서 content. snapshots 는 report_items 행({turn_id, snapshot})을 순서대로"""
    items = report_compare.items_of(snapshots)
    if not items:
        raise ValueError("담긴 결과가 없습니다")
    cmp = report_compare.compare(items)
    allowed = _allowed_numbers(title, purpose, items, cmp)
    messages = [
        {"role": "system", "content": prompt.SYSTEM},
        {
            "role": "user",
            "content": json.dumps(llm_input(title, purpose, items, cmp), ensure_ascii=False),
        },
    ]
    sections, dropped, how, warnings = None, [], get_settings().report_llm, []
    for attempt in range(2):
        try:
            out = await _ask(messages)
        except Exception as e:  # HCX · Gemma 오류: 코드 문장으로 만든다
            logger.warning("보고서 문장 LLM 실패: %s", e)
            warnings.append("자동 분석 문장을 만들지 못해 각 결과의 요약만 실었습니다.")
            break
        kept, problems, dropped = check(out, items, cmp, allowed)
        if not problems or (attempt == 1 and kept["summary"]):
            # 두 번째에도 걸리면 걸린 문장만 빼고 쓴다 (요약이 하나도 안 남으면 대체 문장)
            sections = kept
            if bad_lines := [d for d in dropped if d["why"] in problems]:
                warnings.append(f"검증을 통과하지 못한 문장 {len(bad_lines)}개는 뺐습니다.")
            if missing := set(unvalued(kept, items)):  # 값이 빠진 결과는 코드 요약 문장으로 채운다
                more = [x for x in _fallback(items)["summary"] if x["refs"][0] in missing]
                sections = {**kept, "analysis": kept["analysis"] + more}
            break
        logger.info("보고서 문장 검증 %d회차 문제: %s", attempt + 1, problems)
        messages += [
            {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
            {
                "role": "user",
                "content": prompt.RETRY.format(problems="\n".join(f"- {p}" for p in problems)),
            },
        ]
    else:
        warnings.append("자동 분석 문장이 검증을 통과하지 못해 각 결과의 요약만 실었습니다.")
    if sections is None:
        sections, how = _fallback(items), "fallback"
    values = {it["id"]: report_compare.value_text(it) for it in items}
    values |= {d["id"]: report_compare.value_text(d) for d in cmp["derived"]}
    units = {it["id"]: it["figure"].get("unit") or "" for it in items}
    units |= {d["id"]: d.get("unit") or "" for d in cmp["derived"]}
    return {
        "title": title,
        "purpose": purpose,
        "generated_by": how,
        "summary": [{**s, "text": fill(s["text"], values, units)} for s in sections["summary"]],
        "analysis": [{**s, "text": fill(s["text"], values, units)} for s in sections["analysis"]],
        "limits": cmp["limits"],
        "items": [
            {
                "id": it["id"],
                "turn_id": it["turn_id"],
                "question": it["snapshot"].get("question"),
                "label": it["figure"].get("label"),
                "value_text": values[it["id"]] if it["figure"].get("value") is not None else None,
                "figure": it["figure"],
                "sources": it["snapshot"].get("sources") or [],
                "chart": it["snapshot"].get("chart"),
            }
            for it in items
        ],
        "derived": [{**d, "value_text": values[d["id"]]} for d in cmp["derived"]],
        "not_comparable": cmp["not_comparable"],
        "dropped": dropped,
        "warnings": warnings,
    }
