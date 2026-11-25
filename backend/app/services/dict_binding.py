import logging
import re
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.services import binding, indicators

logger = logging.getLogger(__name__)

KEEP, HAND_OFF = "그대로", "넘김"
POP_ITEMS = {"계": "T2", "남": "T3", "여": "T4"}
HANDLED_DIM = re.compile(r"연령|성별|성\b|행정구역|시도|시군구|지역")
SUB_YEAR = re.compile(r"\d{1,2}\s*월|상반기|하반기|\d\s*분기|분기별|월별")
SEX_CLASS = re.compile(r"^(성별?|남녀|성\s*별)$")
MONTHLY = re.compile(r"월\s*말|월별")
FROM_TO = re.compile(r"\S+에서\s*\S+?(?:으로|로)\s*(?:이사|이동|옮|간|가|온|오|빠져)")
# '다른 시도에서 경기도로 이사 오는 사람'은 특정 경로가 아니라 전입자 수(밖에서 들어온 사람 전체)다
GENERIC_ORIGIN = re.compile(
    r"(?:다른|타|여타|외부|딴)\s*(?:시도|지역|시군구|도시|곳|데)\s*에서|밖에서"
)


def specific_route(question: str) -> bool:
    return bool(FROM_TO.search(question)) and not GENERIC_ORIGIN.search(question)


FILTER_KEY = re.compile(r"c(\d+)_id")
PERIOD_CONDS = ("year", "period")


def _norm(text: str) -> str:
    return re.sub(r"[\s·()\-_]", "", str(text or ""))


def _spec_for(parsed: dict, tbl_id: str) -> dict:
    name = parsed.get("indicator") or ""
    names = [name, *(n for *_, n in indicators.find_in_text(name))]
    for n in names:
        sp = indicators.spec(n)
        if sp and any(s.get("table") == tbl_id for s in sp.get("sources") or []):
            return sp
    return indicators.spec(name) or {}


def _source(
    result: dict, parsed: dict
) -> tuple[str | None, dict[int, str], dict[int, set[str]], dict]:
    from app.services import qa

    src = qa.sources(result)
    tbl_id = src[0]["tbl_id"] if src else None
    if not tbl_id:
        return None, {}, {}, {}
    sp = _spec_for(parsed, tbl_id)
    used = next((s for s in sp.get("sources") or [] if s.get("table") == tbl_id), {})
    forced: dict[int, str] = {}
    if used.get("item_id"):
        forced[0] = used["item_id"]
    elif tbl_id == indicators.POP_TABLE:
        forced[0] = POP_ITEMS.get(parsed.get("sex") or "계", POP_ITEMS["계"])
    decided: dict[int, set[str]] = {no: {code} for no, code in forced.items()}
    for key, value in (used.get("filters") or {}).items():
        if m := FILTER_KEY.fullmatch(key):
            codes = [value] if isinstance(value, str) else list(value)
            decided[int(m[1])] = set(codes)
            if len(codes) == 1:
                forced[int(m[1])] = codes[0]
    return tbl_id, forced, decided, sp


def _indicator_words(sp: dict, parsed: dict) -> list[str]:
    words = [
        sp.get("name"),
        *(sp.get("aliases") or []),
        parsed.get("indicator"),
        parsed.get("term"),
    ]
    return [_norm(w) for w in words if w and len(_norm(w)) >= 2]


def _in_indicator(value: str, words: list[str]) -> bool:
    v = _norm(value)
    return len(v) >= 2 and any(v in w or w in v for w in words)


def _ignored(
    b: "binding.Binding", tbl_id: str, decided: dict[int, set[str]], words: list[str]
) -> list[str]:
    from app.services import dynamic_indicator as di

    names = {d.no: d.name for d in di.schema(tbl_id)[0]}
    out = []
    for x in b.bound:
        if x.dim is None or x.dim == 0:
            continue
        if x.by in ("forced", "total", "default", "region", "period"):
            continue
        if x.dim in decided:
            if x.by != "dim" and set(x.codes or []) <= decided[x.dim]:
                continue
            out.append(f"{x.cond} '{x.value}' → {names.get(x.dim, x.dim)} (사전이 정한 칸과 다름)")
            continue
        if _in_indicator(x.value, words):
            continue
        if x.by == "dim" or not HANDLED_DIM.search(names.get(x.dim, "")):
            out.append(
                f"{x.cond} '{x.value}' → {names.get(x.dim, x.dim)} (지표 사전이 쓰지 않는 칸)"
            )
    return out


def _unbound_reasons(b: "binding.Binding", parsed: dict, words: list[str]) -> list[str]:
    out = []
    for u in b.unbound:
        if u.cond in PERIOD_CONDS:
            continue
        if u.cond == "topic" and parsed.get("grounded") is not False:
            continue
        if u.value and _in_indicator(u.value, words):
            continue
        out.append(f"{u.cond} '{u.value}': {u.why}" if u.value else f"{u.cond}: {u.why}")
    return out


async def _conditions(question: str) -> dict[str, Any]:
    from app.services import search, table_guide
    from app.services import slots as slot_parser

    found = await slot_parser.extract(question, {})
    search.ground_conditions(found)
    return table_guide._understanding(found)


def judge(question: str, parsed: dict, result: dict, u: dict) -> dict[str, Any] | None:
    tbl_id, forced, decided, sp = _source(result, parsed)
    if not tbl_id:
        return None
    cond = dict(u.get("conditions") or {})
    cond["classes"] = [c for c in cond.get("classes") or [] if not SEX_CLASS.match(c)]
    slots = {
        "conditions": cond,
        "target": u.get("target"),
        "topic": u.get("topic"),
        "table_name": result.get("source") or "",
        "table_words": [result.get("source") or "", u.get("target")],
    }
    b = binding.bind(tbl_id, question, {**parsed, "sex": None}, slots, forced)
    words = _indicator_words(sp, parsed)
    reasons = _unbound_reasons(b, parsed, words) + _ignored(b, tbl_id, decided, words)
    basis = str(result.get("basis") or "")
    if SUB_YEAR.search(question) and not result.get("as_of") and not MONTHLY.search(basis):
        reasons.append("월 · 분기 단위를 물었는데 연간 값")
    if specific_route(question):
        reasons.append("'○○에서 ○○로' 옮긴 사람 (전출지 → 전입지)")
    out = b.to_dict()
    out.update(
        tbl_id=tbl_id,
        reasons=reasons,
        decision=HAND_OFF if reasons else KEEP,
        forced={str(k): v for k, v in forced.items()},
        decided={str(k): sorted(v) for k, v in decided.items()},
        grounded=parsed.get("grounded"),
        understanding={k: u.get(k) for k in ("topic", "target", "conditions")},
    )
    return out


async def shadow(question: str, parsed: dict, result: dict) -> dict[str, Any] | None:
    if result.get("status") != "ok" or not question:
        return None
    try:
        u = await _conditions(question)
        return await run_in_threadpool(judge, question, parsed, result, u)
    except Exception as e:
        logger.exception("지표 사전 잇기 기록 실패")
        return {"error": f"{type(e).__name__}: {e}"}
