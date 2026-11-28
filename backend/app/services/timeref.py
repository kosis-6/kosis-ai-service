import re
import sys
from dataclasses import dataclass
from datetime import date

WORDS = [
    ("재작년", -2),
    ("지지난해", -2),
    ("지난해", -1),
    ("작년", -1),
    ("전년도", -1),
    ("올해", 0),
    ("금년", 0),
    ("이번 해", 0),
    ("내후년", 2),
    ("내년", 1),
]
AGO = r"전(?![국라남북주체])"
N_YEARS_AGO = re.compile(
    r"(?<![\d~])(\d{1,2})\s*년\s*" + AGO + r"(?!\s*(?:보다|대비|에 비해))(\s*부터)?"
)
COMPARE_BASE = re.compile(
    r"(재작년|작년|전년|지난해|올해|금년|\d{1,2}\s*년\s*" + AGO + r")\s*(이랑|하고|랑|과|와)?"
    r"\s*(보다|대비|에 비해|에비해|비교)"
)
FROM = re.compile(r"(부터|이후|이래)")
RECENT_N_YEARS = re.compile(
    r"(최근|최신|지난|요즘|과거)\s*(\d{1,2})\s*(?:개년|년)(?!\s*" + AGO + r")"
    r"(?:\s*(?:간|동안|사이))?"
)


def lookup_trend_years(q: dict, lo: int, hi: int, year: int) -> tuple[int, int]:
    asked = q.get("asked") or {}
    if recent := asked.get("recent"):
        return hi - recent + 1, hi
    end = hi if asked.get("since") else min(q.get("end_year") or year, hi)
    if start := q.get("start_year"):
        return start, end
    if asked.get("streak") or asked.get("from_start"):
        return lo, end
    return end - (4 if asked.get("these_days") else 5), end


@dataclass
class TimeRef:
    expr: str
    year: int
    open_end: bool = False

    def describe(self, today: date) -> str:
        return f"'{self.expr}' = {self.year}년 ({today.year}년 기준)"


def resolve(question: str, today: date | None = None) -> TimeRef | None:
    today = today or date.today()
    question = COMPARE_BASE.sub(" ", question)
    if m := N_YEARS_AGO.search(question):
        expr = m.group(0).strip()
        return TimeRef(expr, today.year - int(m.group(1)), open_end=bool(m.group(2)))
    for word, offset in WORDS:
        if (pos := question.find(word)) >= 0:
            tail = question[pos + len(word) : pos + len(word) + 3]
            return TimeRef(word, today.year + offset, open_end=bool(FROM.match(tail.strip())))
    return None


def data_end(today: date | None = None) -> int:
    try:
        from app.services import indicators

        return indicators._data_years()[1]
    except Exception:
        return (today or date.today()).year - 1


def recent_period(
    question: str, today: date | None = None, end: int | None = None
) -> TimeRef | None:
    if m := RECENT_N_YEARS.search(question):
        end = end if end is not None else data_end(today)
        return TimeRef(m.group(0).strip(), end - int(m.group(2)) + 1, open_end=True)
    return None


KINDS = (
    "word",
    "ago",
    "later",
    "recent",
    "span",
    "compare_ago",
    "span_from",
    "span_until",
    "until_now",
)
N_KINDS = ("ago", "later", "recent", "span", "compare_ago", "span_from", "span_until")
_N = r"(?<![\d~])(\d{1,2})\s*"
SPAN_FROM = re.compile(r"((?:19|20)\d{2})\s*년\s*부터\s*(\d{1,2})\s*(?:개년|년)\s*(?:간|동안)")
SPAN_UNTIL = re.compile(
    rf"(?:(?<!\d)(?P<year>(?:19|20)\d{{2}})(?:\s*년도?)?|"
    rf"(?P<relative>{'|'.join(word for word, _ in WORDS)}|(?<!\d)\d{{1,2}}\s*년\s*{AGO}))"
    rf"\s*까지\s*(?:(?:최근|지난|과거)\s*)?(?P<length>[1-9]\d?)\s*(?:개년|년)(?!\s*{AGO})"
    r"(?:\s*(?:간|동안|사이))?"
)
COMPARE_AGO = re.compile(
    _N + r"년\s*" + AGO + r"\s*(?:이랑|하고|랑|과|와)?\s*(?:보다|대비|에\s*비해|비교)"
)
SPAN_N = re.compile(_N + r"(?:개년|년)\s*(?:간|동안)")
LATER_N = re.compile(_N + r"년\s*(?:뒤|후)(?![반기])")
UNTIL_NOW = re.compile(r"(?:지금|현재|요즘|최근)\s*까지")
WORD_AS_BASE = re.compile(r"\s*(?:보다|대비|에\s*비해)")


def find(question: str) -> list[dict]:
    text, out = question, []

    def take(pattern: re.Pattern, kind: str, n_group: int | str | None, **extra) -> None:
        nonlocal text
        for m in pattern.finditer(text):
            ref = {"expr": m.group(0).strip(), "kind": kind}
            ref["n"] = int(m.group(n_group)) if n_group else None
            ref.update({k: (v(m) if callable(v) else v) for k, v in extra.items()})
            out.append(ref)
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)

    take(
        SPAN_UNTIL,
        "span_until",
        "length",
        anchor=lambda m: int(m["year"]) if m["year"] else m["relative"],
    )
    take(SPAN_FROM, "span_from", 2, anchor=lambda m: int(m.group(1)))
    take(RECENT_N_YEARS, "recent", 2)
    take(COMPARE_AGO, "compare_ago", 1)
    take(SPAN_N, "span", 1)
    take(LATER_N, "later", 1)
    take(N_YEARS_AGO, "ago", 1, open=lambda m: bool(m.group(2)))
    take(UNTIL_NOW, "until_now", None)
    bases = []
    for word, offset in WORDS:
        pos = text.find(word)
        if pos < 0:
            continue
        tail = text[pos + len(word) :]
        text = text.replace(word, " " * len(word))
        ref = {
            "expr": word,
            "kind": "word",
            "n": None,
            "offset": offset,
            "open": bool(FROM.match(tail[:3].strip())),
        }
        if WORD_AS_BASE.match(tail):
            bases.append({**ref, "base": True})
            continue
        out.append(ref)
    # '작년보다 늘었어?'의 기준 낱말은 빼고 전년 대비로 센다. 견줄 낱말이 따로 있으면
    # ('재작년보다 작년') 기준도 시점이다: 두 낱말 시점 = 두 해 (11/28 변형셋 P-040이 기준
    # '재작년'을 잃고 한 시점 질문이 되어 2025년 값 하나로 답했다)
    if any(r["kind"] == "word" for r in out):
        out.extend(bases)
    return out


def period(ref: dict, today: date, end: int | None = None) -> dict:
    kind, n = ref["kind"], ref.get("n") or 0
    if kind == "word":
        year = today.year + ref.get("offset", 0)
        return {"start": year, "end": None} if ref.get("open") else {"year": year}
    if kind == "ago":
        year = today.year - n
        return {"start": year, "end": None} if ref.get("open") else {"year": year}
    if kind == "later":
        return {"year": today.year + n}
    if kind in ("recent", "span", "compare_ago"):
        last = end if end is not None else data_end(today)
        return {"start": last - n + (kind != "compare_ago"), "end": None}
    if kind == "span_from":
        return {"start": ref["anchor"], "end": ref["anchor"] + n}
    if kind == "span_until":
        anchor = ref["anchor"]
        last = anchor if isinstance(anchor, int) else resolve(anchor, today).year
        return {"start": last - n + 1, "end": last}
    if kind == "until_now":
        return {"end": None}
    return {}


if __name__ == "__main__":
    for q in sys.argv[1:] or ["작년 출생아 수", "3년 전부터 인구 변화", "작년보다 인구가 늘었어?"]:
        print(q, "→", resolve(q))
