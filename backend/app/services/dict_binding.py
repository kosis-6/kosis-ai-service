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


def _source(result: dict, parsed: dict) -> tuple[str | None, dict[int, str]]:
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
    from app.services import search, table_guide
    from app.services import slots as slot_parser

    found = await slot_parser.extract(question, {})
    search.ground_conditions(found)
    return table_guide._understanding(found)


async def shadow(question: str, parsed: dict, result: dict) -> dict[str, Any] | None:
    if result.get("status") != "ok" or not question:
        return None
    tbl_id, forced = _source(result, parsed)
    if not tbl_id:
        return None
    try:
        u = await _conditions(question)
        cond = dict(u.get("conditions") or {})
        cond["classes"] = [c for c in cond.get("classes") or [] if not SEX_CLASS.match(c)]
        slots = {
            "conditions": cond,
            "target": u.get("target"),
            "topic": u.get("topic"),
            "table_name": result.get("source") or "",
            "table_words": [result.get("source") or "", u.get("target")],
        }
        q = {**parsed, "sex": None}
        b = await run_in_threadpool(binding.bind, tbl_id, question, q, slots, forced)
        ignored = await run_in_threadpool(_ignored, b, tbl_id)
    except Exception as e:
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
