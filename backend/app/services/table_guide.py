import logging
import re
from typing import Any
from uuid import uuid4

from app.agent import graph, multi
from app.core.config import get_settings
from app.services import flow, search, search_answer, search_llm, search_log, synonyms
from app.services import slots as slot_parser

logger = logging.getLogger(__name__)

SEARCH_INSTEAD = {"unknown_indicator", "not_supported_question", "parse_error"}
SEARCH_AFTER = {"missing_data", "no_region_level", "out_of_range", "region_not_in_year"}
REFUSED = ("out_of_scope", "not_statistics")

OFF_DOMAIN_EARLY = re.compile(
    r"아파트\s*값|집\s*값|땅\s*값|공시지가|전셋값|"
    r"(?:아파트|주택|집|빌라|오피스텔|땅|토지|부동산)\S*\s*(?:평균\s*|매매\s*)?(?:가격|시세)|"
    r"실업률|취업률|고용률|연봉|급여|최저임금|"
    r"미세먼지|오염|날씨|강수량|폭염|공기\s*질|공기\S*\s*(?:제일\s*|가장\s*)?(?:안\s*좋|나쁜|나빠)|"
    r"경제\s*성장|GDP|국내총생산|물가|금리|환율|교통량",
    re.IGNORECASE,
)
POPULATION_SIGNAL = re.compile(
    r"인구|가구|세대|출생|태어|아기|아이|애기|사망|돌아가|혼인|결혼|이혼|외국인|이동|전입|전출|이사|"
    r"노인|어르신|고령|청년|주민"
)
TOP_K = 3
MAX_CARDS = 3


def new_search_session() -> str:
    return f"S_{uuid4().hex[:12]}"


async def cards_for(question: str, sources: list[dict]) -> list[dict]:
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
    plan: dict[str, Any] | None = None,
) -> dict:
    """통계표 찾기 한 턴. 되묻기 중이면 이어서 처리한다.
    plan: 재계획(agent/replan.py)이 세운 계획. 있으면 계획 LLM 없이 그 계획으로 지표를 찾는다

    /search(dialog.handle)와 같은 처리를 LangGraph 흐름(app/agent)으로 돌린다.
    새 질문이면 계획이 질문을 지표로 나누고, 지표가 여럿이면 지표마다 찾은 표를
    응답의 multi에 덧붙인다 (SEARCH_PLAN=false면 계획 없이 기존과 같은 결과만)"""
    trace: dict = {}
    result, response, error = None, None, None
    handle = multi.handle if get_settings().search_plan else graph.handle
    try:
        if plan and handle is multi.handle:
            result = await handle(
                question, session_id, selections, trace, user_id=user_id, plan=plan
            )
        else:
            result = await handle(question, session_id, selections, trace, user_id=user_id)
        session_id = trace.get("search_session_id", session_id)
        tables = await search.load_tables()
        response = search_answer.respond(result, tables, session_id, TOP_K, trace)
        by_indicator = indicator_tables(result.get("multi"), tables)
        if by_indicator:
            response["multi"] = by_indicator
        try:
            response["handoff"] = build_handoff(result, response, tables)
        except Exception:
            logger.exception("넘기기 꾸러미(handoff) 만들기 실패")
            response["handoff"] = None
        recs = response.get("recommendations") or []
        picked = " / ".join(f"{r.get('tbl_id')} {r.get('tbl_nm') or ''}".strip() for r in recs[:3])
        flow.add("통계표 찾기", response.get("status"), picked or response.get("answer") or "")
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
        except Exception:
            logger.exception("검색 요청 기록 실패")


def indicator_tables(multi_result: dict | None, tables: list[dict]) -> dict | None:
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


GROUP_INSTEAD = {
    "청년": "청년 인구",
    "노인": "노인 인구",
    "어르신": "노인 인구",
    "고령": "고령인구 비율",
    "외국인": "외국인 주민 수",
    "여성": "여성 인구",
    "남성": "남성 인구",
}


def out_of_scope_text(word: str, question: str) -> str:
    from app.services import indicators
    from app.services.josa import josa

    quoted = f"'{word}'"
    text = f"{josa(quoted, '은는')} 제공 범위(KOSIS 인구 분야 통계)에 없는 통계라 답할 수 없습니다."
    group = next((g for g in GROUP_INSTEAD if g in question), None)
    if group is None:
        return f"{text} 인구 · 가구 · 출생 · 사망 · 혼인 · 인구 이동 통계로 질문해 주세요."
    try:
        region = next(iter(indicators.find_regions(question)), "서울")
    except Exception:  # noqa: BLE001 - 지역 목록을 못 읽어도 안내는 한다
        region = "서울"
    instead = GROUP_INSTEAD[group]
    told = josa(f"'{instead}'", "은는")
    return f"{text} 대신 {told} 볼 수 있습니다. 예) {region} {josa(instead, '은는')}?"


async def off_domain_early(question: str) -> dict | None:
    from app.services import indicators
    from app.services.llm_pick import OFF_DOMAIN

    words = [m.group(0) for rx in (OFF_DOMAIN, OFF_DOMAIN_EARLY) for m in rx.finditer(question)]
    if not words:
        return None
    rest = question
    for word in words:
        rest = re.sub(rf"\S*\s*{re.escape(word)}", " ", rest)
    population = bool(POPULATION_SIGNAL.search(rest) or indicators.find_in_text(question))
    asked = coordinated_words(question, words) if population else {}
    if population and not asked:
        return None
    tables = await search.load_tables()
    catalog = [t["name_n"] + t["meta_n"] + t["path_n"] for t in tables]
    for word in words:
        n = search.norm(asked_word(asked.get(word, question), word))
        if any(n in text for text in catalog):
            continue
        if population and word not in asked:
            continue
        from app.services.josa import josa

        if population:
            from app.services import refusal

            inside = list(dict.fromkeys(name for *_, name in indicators.find_in_text(question)))
            answer = refusal._outside_text([asked[word]], inside)
            said = josa(f"'{asked[word]}'", "은는")
            reason = f"{said} 인구 통계 범위 밖입니다 (함께 물은 지표, LLM 호출 전 검사)"
        else:
            answer = out_of_scope_text(word, question)
            said = josa(f"'{word}'", "은는")
            reason = f"{said} 인구 통계 범위 밖입니다 (LLM 호출 전 단어 검사)"
        found = {
            "session_id": None,
            "status": "out_of_scope",
            "answer": answer,
            "parsed": {},
            "clarify": None,
            "recommendations": [],
            "together": [],
            "notice": None,
            "reason": reason,
        }
        return tables_answer(found)
    return None


PHRASE_JOSA = re.compile(r"(?:을|를|은|는|이|가|도|의)$")


def coordinated_words(question: str, words: list[str]) -> dict[str, str]:
    tokens = synonyms.tokenize(question)
    joins = [t for t in tokens if t.tag == "JC" or t.form in {",", "및", "그리고"}]
    out = {}
    bounds = [0, *(join.end for join in joins), len(question)]
    for i, join in enumerate(joins):
        left = question[bounds[i] : join.start].rstrip()
        right = question[join.end : joins[i + 1].start if i + 1 < len(joins) else bounds[-1]]
        for word in words:
            if word in out:
                continue
            phrase = rf"(?:[^\s,]+\s+)?[^\s,]*{re.escape(word)}[^\s,]*"
            if m := re.search(rf"{phrase}$", left):
                out[word] = _trim_phrase(m[0], word)
            elif (m := re.match(rf"\s*({phrase})", right)) and not _modifies_next(
                tokens, join.end + m.end()
            ):
                out[word] = _trim_phrase(m[1], word)
    return out


def _trim_phrase(phrase: str, word: str) -> str:
    phrase = phrase.rstrip("?.!~,")
    trimmed = PHRASE_JOSA.sub("", phrase)
    return trimmed if word in trimmed else phrase


def asked_word(question: str, word: str) -> str:
    i = question.find(word)
    if i < 0:
        return word
    j = i + len(word)
    while j < len(question) and not question[j].isspace():
        j += 1
    return _trim_phrase(question[i:j], word)


def _modifies_next(tokens: list, end: int) -> bool:
    tail = [t for t in tokens if t.start >= end]
    noun = ("NN", "NP", "NR", "SN")
    for token, following in zip(tail, tail[1:], strict=False):
        if token.tag == "ETM":
            return following.tag.startswith((*noun, "MM"))
        if token.tag in {"JC", "MAJ", "SP", "SF", "EF"}:
            return False
        if token.tag.startswith(noun) and not (
            following.tag == "JKB" and token.end == following.start
        ):
            return False
    return False


def waiting_choice(found: dict | None) -> bool:
    return bool(found and found.get("status") in ("clarify", "ambiguous"))


HANDOFF_VERSION = 1
MAX_CANDIDATES = 5
COMPUTABLE = ("ok", "partial_match")
REGION_SUFFIX = re.compile(r"(특별자치시|특별자치도|특별시|광역시|도|시|군|구)$")
SEX_SAID = {
    "남": re.compile(r"남(자|성|녀)|(?<![구특])성별"),
    "여": re.compile(r"여(자|성)|남녀|(?<![구특])성별"),
}
YEAR_SAID = re.compile(r"(?:19|20)\d{2}(?!\d|\s*세대)")


def build_handoff(
    result: dict[str, Any], response: dict[str, Any], tables: list[dict[str, Any]]
) -> dict[str, Any]:
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
    whole: list[dict[str, Any]] = []
    if status in COMPUTABLE and (result.get("table") or {}).get("tbl_id") in by_id:
        picked_id = result["table"]["tbl_id"]
        if multi_result.get("mode") == "multi":
            targets = [
                _indicator_target(ind, understanding, by_id, families, picked_id)
                for ind in multi_result.get("indicators") or []
            ]
            # 질문 전체 검색 상위 표: 지표를 둘로 나눈 비율 질문에서 분자 · 분모가 한 표에 다 있는
            # 표(1인 가구와 전체 가구)는 지표별 후보 어디에도 없었다 (11/25, 한 표 비율)
            whole = _whole_candidates(result, by_id, families, slots, picked_id)
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
        "whole": whole,  # 지표가 여럿일 때 질문 전체로 찾은 상위 표 (한 표 비율 후보)
        # 계획 전체 (shared · indicators · combine · given). 재계획이 처음 계획으로 쓴다
        "plan": {k: plan.get(k) for k in ("shared", "indicators", "combine", "given") if k in plan},
        "combine": {
            "op": combine.get("op") or "none",
            "note": combine.get("note"),
            "num": combine.get("num") or None,
            "den": combine.get("den") or None,
            "out_of_scope": list(plan.get("out_of_scope") or []),
            # 재계획(agent/replan.py)이 내는 칸: which_larger의 pair, sum의 over, rank의 within·axis
            **{
                k: combine.get(k)
                for k in ("pair", "over", "within", "axis", "scale", "by_change")
                if combine.get(k) not in (None, "", False)
            },
        },
        "clarify": response.get("clarify"),
        "llm": _llm_used(multi_result, pick),
        "ambiguity": None,
    }


def _understanding(slots: dict[str, Any]) -> dict[str, Any]:
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
    card = search_answer.recommendation(rank, t, family, checks, score, slots)
    return {
        "rank": rank,
        "tbl_id": t["tbl_id"],
        "org_id": t["org_id"],
        "tbl_nm": t["table_name"],
        "family_id": t["family_id"],
        "start_year": t.get("start_year"),
        "end_year": t.get("end_year"),
        "target": t.get("target"),
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


def _whole_candidates(
    result: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    families: dict[str, list[dict[str, Any]]],
    slots: dict[str, Any],
    picked_id: str,
) -> list[dict[str, Any]]:
    """질문 전체 검색의 상위 표(탐색이 고른 표 먼저, 그 뒤 다른 묶음)를 후보 모양으로"""
    chosen = by_id[picked_id]
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
    seen = {chosen["family_id"]}
    for c in (result.get("multi") or {}).get("whole") or []:
        t = by_id.get(c["tbl_id"])
        if t is None or t["family_id"] in seen:
            continue
        seen.add(t["family_id"])
        checks = c.get("checks")
        if checks is None:
            checks = search.check(t, slots) if slots.get("conditions") else {}
        out.append(
            _candidate(
                len(out) + 1, t, families.get(t["family_id"], [t]), by_id, checks,
                c.get("score"), slots, "whole", False, None,
            )
        )  # fmt: skip
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def _single_target(
    result: dict[str, Any],
    understanding: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    families: dict[str, list[dict[str, Any]]],
    slots: dict[str, Any],
) -> dict[str, Any]:
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
        if checks is None:
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
        "dict": ind.get("dict"),  # 계획이 적은(또는 plan.fill_dict가 채운) 사전 지표 이름
        "conditions": _plan_conditions(ind.get("conditions") or {}, understanding["conditions"]),
        "question": question,
        "candidates": out,
        "together": [],
    }


def _plan_conditions(cond: dict[str, Any], whole: dict[str, Any]) -> dict[str, Any]:
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
    configured = "hcx" if search_llm.falling_back() else search_llm.provider()
    plan = multi_result.get("plan") or {}
    together = pick.get("together") or {}
    return {
        "slots": configured,
        "plan": configured if multi_result and not plan.get("error") else None,
        "review": pick.get("llm") if pick.get("raw") else None,
        "together": "gemma" if together and not together.get("error") else None,
    }
