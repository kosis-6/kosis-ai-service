import re
from typing import Any

from app.services import slots, timeref

VAGUE_PAST = re.compile(r"과거|예전|옛날|옛적")
FROM_PAST = re.compile(r"(과거|예전|옛날|옛적)\s*부터")
VAGUE_WITH_PARTICLE = re.compile(r"(과거|예전|옛날|옛적)(와|랑|를|가|는|)")
PARTICLE_AFTER_NYEON = {"와": "과", "랑": "이랑", "를": "을", "가": "이", "는": "은"}
FOLLOW_UP_MAX_CHARS = 25
REPLY_FILLER = re.compile(
    r"\s+|[,.·~\-?!/()]|이랑|랑|하고|과|와|부터|까지|에서|이고|그리고|및|도|으로|로|"
    r"입니다|이에요|예요|이요|요|기준|비교|대비|보다|년도|년|쯤|정도"
)
CENSUS_EVERY = 5


def has_explicit_year(question: str) -> bool:
    return bool(
        slots.YEAR.search(question)
        or slots.short_years(question)
        or slots.decades(question)
        or timeref.resolve(question)
        or timeref.RECENT_N_YEARS.search(question)
        or timeref.N_YEARS_AGO.search(question)
    )


def needs_year(question: str) -> bool:
    return (
        bool(VAGUE_PAST.search(question))
        and not has_explicit_year(question)
        and not FROM_PAST.search(question)
    )


def _span_text(spans: list[tuple[int, int]]) -> str:
    merged: list[list[int]] = []
    for lo, hi in sorted(set(spans)):
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    parts, i = [], 0
    while i < len(merged):
        lo, hi = merged[i]
        j = i
        while (
            lo == hi
            and j + 1 < len(merged)
            and merged[j + 1][0] == merged[j + 1][1] == merged[j][0] + CENSUS_EVERY
        ):
            j += 1
        if j - i >= 2:
            parts.append(f"{lo}~{merged[j][0]}년({CENSUS_EVERY}년마다)")
        else:
            j = i
            parts.append(f"{lo}년" if lo == hi else f"{lo}~{hi}년")
        i = j + 1
    return ", ".join(parts)


def missing_year(parsed: dict[str, Any]) -> dict[str, Any]:
    from app.services import indicators, lookup

    name = parsed.get("indicator") or ""
    out: dict[str, Any] = {
        "status": "missing_year",
        "indicator": name or None,
        "reason": "과거 어느 연도와 비교할지 질문에 없습니다.",
    }
    sp = indicators.spec(name) if name else None
    if sp is None:
        return out
    spans, tables = [], []
    if sp["method"] == "lookup":
        for s in sp["sources"]:
            tables.append(s["table"])
            try:
                spans.append(lookup._years(lookup._effective(s, "계") or s))
            except Exception:  # noqa: BLE001 - 적재 안 된 표는 기간 안내에서 뺀다
                continue
    else:
        spans.append(indicators._data_years())
        tables += [s["table"] for s in sp.get("sources") or []]
    if spans:
        out["available"] = _span_text(spans)
        out["available_years"] = [min(a for a, _ in spans), max(b for _, b in spans)]
    marks = ", ".join("?" for _ in tables)
    rows = indicators._catalog_rows(
        f"select min(start_year) as lo from table_meta where tbl_id in ({marks})", tables
    )
    first = rows[0]["lo"] if rows else None
    if first and spans and first < out["available_years"][0]:
        out["kosis_first_year"] = first
    return out


def guide(r: dict[str, Any]) -> str:
    name = r.get("indicator")
    if not r.get("available"):
        return "비교할 연도를 넣어 다시 질문해 주세요 (예: 2010년과 2025년)."
    lo, hi = r["available_years"]
    text = f"{name} 자료는 {r['available']}이 있습니다."
    if r.get("kosis_first_year"):
        text += f" KOSIS에는 {r['kosis_first_year']}년부터 있어, 그 연도를 물으시면 받아 옵니다."
    return text + f" 비교할 연도를 알려 주세요 (예: {lo}년과 {hi}년)."


def merge(pending: str, reply: str) -> str | None:
    reply = reply.strip()
    if len(reply) > FOLLOW_UP_MAX_CHARS or not has_explicit_year(reply):
        return None
    if not _only_years(reply):
        return None
    years = sorted({int(y) for y in slots.YEAR.findall(reply)} | slots.short_years(reply))
    if len(years) == 1 and VAGUE_PAST.search(pending):
        return VAGUE_WITH_PARTICLE.sub(
            lambda m: f"{years[0]}년" + PARTICLE_AFTER_NYEON.get(m.group(2), m.group(2)),
            pending,
            count=1,
        )
    return f"{pending} ({reply})"


def _only_years(reply: str) -> bool:
    rest = slots.DECADE.sub(" ", reply)
    rest = slots.SHORT_DECADE.sub(" ", rest)
    rest = slots.YEAR.sub(" ", rest)
    rest = slots.SHORT_YEAR.sub(" ", rest).replace("'", " ")
    rest = timeref.RECENT_N_YEARS.sub(" ", rest)
    rest = timeref.N_YEARS_AGO.sub(" ", rest)
    for word, _ in timeref.WORDS:
        rest = rest.replace(word, " ")
    return not REPLY_FILLER.sub("", rest)
