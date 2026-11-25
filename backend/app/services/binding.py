import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.services import agegroup, lookup, search
from app.services import dynamic_indicator as di

logger = logging.getLogger(__name__)

BOUND, PARTIAL = "bound", "partial"
MISSED_AGE = "질문 이해에 없는 연령"

NATIVE_COUNT = {
    "하나": 1, "둘": 2, "셋": 3, "넷": 4, "다섯": 5,
    "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}  # fmt: skip
SHORT_COUNT = {"한": 1, "두": 2, "세": 3, "네": 4}
COUNT_NOUN = r"(?:명|자녀|아이|개|가지|번|째)"
_NUMBER = (
    r"\d+(?!\d)|" + "|".join(NATIVE_COUNT)
    + r"|(?:" + "|".join(SHORT_COUNT) + r")(?=\s*" + COUNT_NOUN + r")"
)  # fmt: skip
COUNT_ASK = re.compile(
    r"(?<!\d)(?P<n>" + _NUMBER + r")\s*(?P<noun>" + COUNT_NOUN + r")?"
    r"\s*(?P<op>이상|넘|초과|이하|미만)?(?!\s*(?:살|세|대|년|월|일|%|퍼센트))"
)  # fmt: skip
COUNT_CELL = re.compile(r"(\d+)\s*(?:명|자녀|인|개|가구|회)?\s*(이상)?")
AGE_CELL = re.compile(r"\d+\s*(?:세|살|대)")
MIN_COUNT_CELLS = 3


@dataclass
class Bound:
    cond: str
    value: str
    by: str
    dim: int | None = None
    codes: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Unbound:
    cond: str
    value: str
    why: str


@dataclass
class Binding:
    tbl_id: str
    bound: list[Bound] = field(default_factory=list)
    unbound: list[Unbound] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        return PARTIAL if self.unbound else BOUND

    def picks(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for b in self.bound:
            if b.dim is not None and b.codes:
                out.setdefault(b.dim, [])
                out[b.dim] += [c for c in b.codes if c not in out[b.dim]]
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "bound": [_compact(asdict(b)) for b in self.bound],
            "unbound": [asdict(u) for u in self.unbound],
            **({"skipped": self.skipped} if self.skipped else {}),
        }


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [])}


def _age_text(text: str) -> str:
    return agegroup.native_to_digits(re.sub(r"(\d+)\s*살", r"\1세", text or ""))


def _age_span(text: str) -> tuple[int, int | None] | None:
    t = _age_text(text)
    if a := agegroup.span(t):
        return a.lo, a.hi
    if one := agegroup.single_age(t):
        n = int(one.removesuffix("세"))
        return n, n
    return None


def _count_ask(text: str) -> tuple[int, str] | None:
    m = COUNT_ASK.search(text or "")
    if not m:
        return None
    raw = m["n"]
    n = int(raw) if raw.isdigit() else NATIVE_COUNT.get(raw) or SHORT_COUNT[raw]
    op = {"이상": ">=", "넘": ">", "초과": ">", "이하": "<=", "미만": "<"}.get(m["op"] or "", "==")
    return n, op


def _count_cells(ask: tuple[int, str], d: di.Dim) -> list[str]:
    nums = {}
    for code, name in d.codes.items():
        if AGE_CELL.search(name) or search.norm(name).endswith("계"):
            continue
        if m := COUNT_CELL.search(name):
            nums[code] = (int(m[1]), bool(m[2]))
    if len(nums) < MIN_COUNT_CELLS:
        return []
    n, op = ask
    ok = {
        ">=": lambda v, open_: v >= n,
        ">": lambda v, open_: v > n,
        "<=": lambda v, open_: v <= n and not open_,
        "<": lambda v, open_: v < n and not open_,
        "==": lambda v, open_: v == n and not open_,
    }[op]
    return [c for c, (v, open_) in nums.items() if ok(v, open_)]


MIN_OVERLAP = 0.5


def _related(word: str, name: str) -> bool:
    a, b = search.norm(word), search.norm(name)
    if len(a) < 2 or len(b) < 2 or not (a in b or b in a):
        return False
    return min(len(a), len(b)) / max(len(a), len(b)) >= MIN_OVERLAP


def bind(
    tbl_id: str,
    question: str,
    parsed: dict,
    slots: dict | None,
    forced: dict[int, str] | None = None,
) -> Binding:
    dims, region_dim, _ = di.schema(tbl_id)
    info = lookup._table(tbl_id)
    name_n = search.norm(info.get("tbl_nm") or "")
    titles = [name_n] + [search.norm(w) for w in (slots or {}).get("table_words") or [] if w]
    cond = (slots or {}).get("conditions") or {}
    out = Binding(tbl_id)
    by_no = {d.no: d for d in dims}

    sex_dim = next((d for d in dims if d.no != region_dim and d.sex_codes()), None)
    age_dim = next(
        (d for d in dims if d.no not in (0, region_dim) and d.is_age() and d.total()), None
    )
    sex = parsed.get("sex")
    sex = sex if sex in ("남", "여") else "계"
    try:
        couple = di.couple_counted(tbl_id)
    except Exception:
        logger.exception("부부 단위 표 확인 실패: %s", tbl_id)
        couple = False
    forced = {no: code for no, code in (forced or {}).items() if no in by_no}
    for no, code in list(forced.items()):
        d = by_no[no]
        if code not in d.codes:
            continue
        default = d.default()
        said = bool(di._text_hits(question, [d], set()))
        if default is None and _is_scenario(d) and not said:
            why = "시나리오를 말하지 않았는데 기본(중위) 추계가 없는 표"
            out.unbound.append(Unbound(d.name, d.codes[code], why))
            continue
        if default is not None and code != default and not said:
            forced[no] = code = default
            name = d.codes[code]
            out.bound.append(Bound(d.name, name, "default", no, [code], [name], note="LLM 칸 대신"))
            continue
        out.bound.append(Bound("forced", d.codes[code], "forced", no, [code], [d.codes[code]]))
    ages = parsed.get("ages") or []
    age = tuple(ages[0]) if ages else None

    if sex in ("남", "여") and not (sex_dim is not None and sex_dim.no in forced):
        if sex_dim is not None:
            code = sex_dim.sex_codes()[sex]
            out.bound.append(Bound("sex", sex, "value", sex_dim.no, [code], [sex_dim.codes[code]]))
        elif sex == "여" and search.FEMALE_ONLY_TABLE.search(name_n):
            out.bound.append(Bound("sex", sex, "table", note="표 전체가 여성"))
        else:
            out.unbound.append(Unbound("sex", sex, "표에 성별 구분이 없음"))

    if age:
        label = _age_label(age)
        if age_dim is not None:
            out.bound.append(Bound("age", label, "age_range", age_dim.no, note=age_dim.name))
        elif parsed.get("age_group"):
            out.skipped.append(f"age {label} (지표 이름의 일부로 봄)")
        elif cells := _age_cells(age, dims, region_dim):
            no, codes = cells
            d = by_no[no]
            if forced.get(no) in codes:
                code = forced[no]
                out.bound.append(Bound("age", label, "forced", no, [code], [d.codes[code]]))
            elif len(codes) == 1:
                out.bound.append(Bound("age", label, "value", no, codes, [d.codes[codes[0]]]))
            else:
                names = ", ".join(d.codes[c] for c in codes)
                out.unbound.append(Unbound("age", label, f"그 연령이 든 칸이 여럿 ({names})"))
        elif _age_span(info.get("tbl_nm") or "") == age:
            out.bound.append(Bound("age", label, "table", note="표 이름의 연령 범위"))
        else:
            out.unbound.append(Unbound("age", label, "표에 연령 구분이 없음"))

    skip = {region_dim} | ({age_dim.no} if age_dim else set())
    hits = di._text_hits(question, dims, skip)
    used: set[tuple[int, str]] = set()

    for item in cond.get("items") or []:
        if b := _bind_item(item, dims, skip, hits, titles, age, age_dim, out):
            out.bound.append(b)
            used |= {(b.dim, c) for c in b.codes if b.dim is not None}
        elif b is None:
            out.unbound.append(Unbound("item", item, "맞는 칸 없음"))

    for cls in cond.get("classes") or []:
        d = _class_dim(cls, dims, region_dim, sex_dim, age_dim)
        aged = any(x.cond == "age" or x.by == "age_range" for x in out.bound)
        if d is not None and d is age_dim and aged:
            out.bound.append(Bound("class", cls, "age_range", d.no, note="연령 조건으로"))
            continue
        if d is None:
            out.unbound.append(Unbound("class", cls, "그 분류가 없음"))
        else:
            out.bound.append(Bound("class", cls, "dim", d.no, note=d.name))

    _bind_region(parsed, cond, region_dim, name_n, out)
    _bind_year(parsed, info, out)
    for key in ("exclude", "target", "spouse"):
        if (slots or {}).get(key):
            out.skipped.append(f"{key} {slots[key]}")

    taken = {b.dim for b in out.bound if b.dim is not None}
    multi = di._multi_op(question)
    for d in dims:
        if d.no in skip or d.no in taken:
            continue
        codes = [c for c in hits.get(d.no, []) if (d.no, c) not in used]
        if d is sex_dim:
            _bind_sex_default(d, codes, couple, age, out)
        elif len(codes) > 1 and d.total() and set(codes) == set(di._leaf_codes(d)):
            total = d.total()
            names = " · ".join(d.codes[c] for c in codes)
            out.bound.append(Bound("word", names, "total", d.no, [total], [d.codes[total]]))
        elif len(codes) == 1 or (codes and multi):
            names = [d.codes[c] for c in codes]
            out.bound.append(Bound("word", " · ".join(names), "value", d.no, codes, names))
        elif codes:
            names = ", ".join(d.codes[c] for c in codes)
            out.unbound.append(Unbound(d.name, names, "칸이 여럿 (되묻기)"))
        else:
            _bind_default(d, couple, age, out)
    _bind_topic(slots, parsed, sex, sex_dim, out)
    return out


MATCHED_BY = {"value", "forced", "dim"}


def _bind_topic(slots, parsed, sex, sex_dim, out: Binding) -> None:
    linked = {"item", "class"}
    if any(
        b.by in MATCHED_BY or (b.by in ("table", "age_range") and b.cond in linked)
        for b in out.bound
    ):
        return
    if sex in ("남", "여") and sex_dim is not None:
        return
    topic = (slots or {}).get("topic") or parsed.get("term") or parsed.get("indicator") or ""
    out.unbound.append(Unbound("topic", topic, "질문의 말과 맞는 칸이 없음"))


def _is_scenario(d: "di.Dim") -> bool:
    names = [search.norm(n) for n in d.codes.values()]
    return len(names) >= 2 and sum("추계" in n for n in names) * 2 > len(names)


def _age_cells(age: tuple, dims: list, region_dim: int | None) -> tuple[int, list[str]] | None:
    for d in dims:
        if d.no == region_dim:
            continue
        codes = [c for c, n in d.codes.items() if _age_span(n) == age]
        if codes:
            return d.no, codes
    return None


def _within(span: tuple, age: tuple) -> bool:
    lo, hi = age
    s_lo, s_hi = span
    return lo <= s_lo and (hi is None or (s_hi is not None and s_hi <= hi))


def _age_label(age: tuple) -> str:
    lo, hi = age
    return f"{lo}세 이상" if hi is None else f"{lo}~{hi}세"


def _bind_item(item, dims, skip, hits, titles, age, age_dim, out) -> "Bound | None | bool":
    key = search.norm(item)
    words = _words(item) or ([key] if len(key) >= 2 else [])
    for b in out.bound:
        if b.by == "forced" and words and all(w in search.norm(b.names[0]) for w in words):
            return Bound("item", item, "forced", b.dim, list(b.codes), list(b.names))
    for no, codes in hits.items():
        d = next(x for x in dims if x.no == no)
        near = [c for c in codes if _related(item, d.codes[c])]
        if near:
            return Bound("item", item, "value", no, near, [d.codes[c] for c in near])
    if ask := _count_ask(item):
        for d in dims:
            if d.no in skip:
                continue
            if found := _count_cells(ask, d):
                names = [d.codes[c] for c in found]
                return Bound("item", item, "value", d.no, found, names, note="수 범위")
    words = _words(item)
    if len(words) >= 2:
        cover = [
            (d.no, c)
            for d in dims
            if d.no not in skip
            for c, n in d.codes.items()
            if all(w in search.norm(n) for w in words)
        ]
        if len(cover) == 1:
            no, code = cover[0]
            name = next(x for x in dims if x.no == no).codes[code]
            return Bound("item", item, "value", no, [code], [name], note="낱말 모두 포함")
    for d in dims:
        if d.no in skip:
            continue
        same = [c for c, n in d.codes.items() if search.stem(n) == search.stem(key)]
        if len(same) == 1:
            return Bound("item", item, "value", d.no, same, [d.codes[same[0]]], note="낱말 줄기")
    if (span := _age_span(item)) is not None:
        if age is not None and _within(span, age):
            return False
        if age is None and age_dim is not None:
            return Bound("item", item, "age_range", age_dim.no, note=MISSED_AGE)
        if span == _age_span(titles[0]):
            return Bound("item", item, "table", note="표 이름의 연령 범위")
    if len(key) >= 2 and any(key in t for t in titles):
        return Bound("item", item, "table", note="표 이름")
    return None


JOINERS = {"및", "와", "과", "랑", "이랑", "하고", "또는", "그리고", "중", "의"}


def _words(text: str) -> list[str]:
    parts = re.split(r"[^0-9A-Za-z가-힣]+", text or "")
    return [search.norm(p) for p in parts if len(search.norm(p)) >= 2 and p not in JOINERS]


def _class_dim(cls, dims, region_dim, sex_dim, age_dim) -> "di.Dim | None":
    raw = search.norm(cls)
    if raw in ("성별", "성", "남녀", "남녀별"):
        return sex_dim
    key = raw.removesuffix("별")
    key = search.CLASS_ALIAS.get(key, key)
    if key == "연령":
        return age_dim
    return next(
        (d for d in dims if d.no != region_dim and key and key in search.norm(d.name)), None
    )


def _bind_region(parsed, cond, region_dim, name_n, out) -> None:
    regions = parsed.get("regions") or ([cond["region"]] if cond.get("region") else [])
    for r in regions:
        if region_dim is not None:
            out.bound.append(Bound("region", r, "region", region_dim))
        elif r == "전국":
            out.bound.append(Bound("region", r, "table", note="전국 표"))
        elif search.norm(search.REGION_SHORT.get(r, r)) in name_n:
            out.bound.append(Bound("region", r, "table", note="그 지역만 다루는 표"))
        else:
            out.unbound.append(Unbound("region", r, "표에 지역 구분이 없음"))


def _bind_year(parsed, info, out) -> None:
    y0 = parsed.get("start_year") or parsed.get("year")
    y1 = parsed.get("end_year") or parsed.get("year")
    lo, hi = info.get("start_year"), info.get("end_year")
    if not y0:
        return
    label = f"{y0}" if y0 == y1 else f"{y0}~{y1}"
    if lo is None or hi is None:
        out.skipped.append(f"year {label} (표의 수록 기간 모름)")
    elif lo <= y0 and (y1 or y0) <= hi:
        out.bound.append(Bound("year", label, "period", note=f"{lo}~{hi}"))
    else:
        out.unbound.append(Unbound("year", label, f"수록 기간 밖 ({lo}~{hi})"))


def _bind_sex_default(d, codes, couple, age, out) -> None:
    sex_set = set(d.sex_codes().values())
    others = [c for c in codes if c not in sex_set]
    if any(b.cond == "sex" for b in out.bound):
        return
    if others:
        out.bound.append(Bound("word", d.codes[others[0]], "value", d.no, others[:1]))
        return
    if (total := d.total()) is not None:
        out.bound.append(Bound(d.name, d.codes[total], "total", d.no, [total], [d.codes[total]]))
        return
    rest = [c for c in d.codes if c not in sex_set]
    if len(rest) == 1:
        out.bound.append(Bound(d.name, d.codes[rest[0]], "total", d.no, rest, [d.codes[rest[0]]]))
        return
    husband = (di._couple_code(d) or d.sex_codes()["남"]) if couple else None
    if husband and not age:
        name = d.codes[husband]
        out.bound.append(Bound(d.name, name, "couple", d.no, [husband], [name]))
        return
    both = [d.sex_codes()["남"], d.sex_codes()["여"]]
    out.bound.append(Bound(d.name, "남 · 여", "split", d.no, both, [d.codes[c] for c in both]))


def _bind_default(d: "di.Dim", couple: bool, age, out: Binding) -> None:
    if (code := d.total()) is not None:
        by = "total"
    elif (code := d.default()) is not None:
        by = "default"
    elif len(d.codes) == 1:
        code, by = next(iter(d.codes)), "total"
    elif couple and (code := di._couple_code(d)) and not age:
        by = "couple"
    else:
        out.unbound.append(Unbound(d.name, "", "합계 칸이 없음 (되묻기)"))
        return
    out.bound.append(Bound(d.name, d.codes[code], by, d.no, [code], [d.codes[code]]))


SAME, DIFFER = "같음", "다름"
NOT_COMPARED = "견주지 않음 (비율 · 구성비)"
CHECK_BLOCKED = "칸 같음 · 계산 검사에서 막힘"
ONLY_BINDING, ONLY_CURRENT, BOTH_FAIL = "잇기만 성공", "지금 계산만 성공", "둘 다 실패"


def current_picks(plan: "di.Plan | None") -> dict[int, list[str]]:
    if plan is None:
        return {}
    out = {no: [code] for no, code in plan.pick.items()}
    if plan.multi:
        no, codes, _ = plan.multi
        out[no] = list(codes)
    if plan.split_sex and plan.sex_dim is not None:
        out[plan.sex_dim.no] = [plan.sex_dim.sex_codes()[s] for s in ("남", "여")]
    return out


def compare(b: Binding, plan: "di.Plan | None", ok: bool) -> dict[str, Any]:
    bound = b.status == BOUND
    if not ok and (plan is None or not bound):
        return {"vs_current": ONLY_BINDING if bound else BOTH_FAIL}
    if not bound:
        return {"vs_current": ONLY_CURRENT}
    cur, mine = current_picks(plan), b.picks()
    dims = {d.no: d for d in di.schema(b.tbl_id)[0]}
    diff = [
        {"dim": dims[x.dim].name, "current": ["연령 조건 없음"], "binding": [x.value]}
        for x in b.bound
        if x.note == MISSED_AGE and x.dim in dims
    ]
    for no in sorted(set(cur) | set(mine)):
        if no not in cur or no not in mine or set(cur[no]) == set(mine[no]):
            continue
        d = dims.get(no)
        names = d.codes if d else {}
        diff.append(
            {
                "dim": d.name if d else no,
                "current": [names.get(c, c) for c in cur[no]],
                "binding": [names.get(c, c) for c in mine[no]],
            }
        )
    if diff:
        return {"vs_current": DIFFER, "diff": diff}
    return {"vs_current": SAME if ok else CHECK_BLOCKED}


def shadow(
    tbl_id: str,
    question: str,
    parsed: dict,
    slots: dict | None,
    plan,
    ok: bool,
    forced: dict[int, str] | None = None,
) -> dict[str, Any] | None:
    try:
        b = bind(tbl_id, question, parsed, slots, forced)
        if parsed.get("type") == "composition" or di.PCT_ASK.search(question):
            return {**b.to_dict(), "vs_current": NOT_COMPARED}
        return {**b.to_dict(), **compare(b, plan, ok)}
    except di.StatsTableMissing:
        return None
    except Exception:
        logger.exception("잇기 기록 실패: %s", tbl_id)
        return {"error": "잇기 실패"}


RANK_SKIP = re.compile(r"기타|미상")


QUIET = ("total", "default", "couple")
RANK_ASK = re.compile(r"\d+\s*위|순위|제일|가장")


@dataclass
class Retry:
    forced: dict[int, str]
    multi: tuple[int, list[str], str] | None
    parsed: dict
    quiet: dict[int, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "forced": self.forced,
            "multi": list(self.multi) if self.multi else None,
            "quiet": self.quiet,
        }


def retry_plan(b: Binding, question: str, parsed: dict, ratio_ok: bool = False) -> Retry | None:
    if b.status != BOUND:
        return None
    ratio = parsed.get("type") == "composition" or bool(di.PCT_ASK.search(question))
    if ratio and not ratio_ok:
        return None
    dims = {d.no: d for d in di.schema(b.tbl_id)[0]}
    forced: dict[int, str] = {}
    quiet: dict[int, str] = {}
    multi = None
    q = dict(parsed)
    for x in b.bound:
        if x.by == "table" and x.cond == "sex":
            q["sex"] = None
        if x.by in ("table", "value", "forced") and x.cond == "age":
            q["ages"] = None
        if x.note == MISSED_AGE and (span := _age_span(x.value)):
            q["ages"] = [list(span)]
        if x.dim is None or x.by in ("age_range", "region", "split"):
            continue
        if x.by == "dim":
            d = dims[x.dim]
            rank = (
                parsed.get("type") == "rank"
                or bool(di.TOP_N.search(question))
                or bool(RANK_ASK.search(question))
            )
            codes = [c for c in di._leaf_codes(d) if not (rank and RANK_SKIP.search(d.codes[c]))]
            group = (x.dim, codes, "rank" if rank else "compare")
        elif len(x.codes) > 1:
            group = (x.dim, list(x.codes), di._multi_op(question) or "sum")
        else:
            if forced.get(x.dim, x.codes[0]) != x.codes[0]:
                return None
            forced[x.dim] = x.codes[0]
            if x.by in QUIET:
                quiet[x.dim] = x.by
            continue
        if multi is not None and multi[0] != group[0]:
            return None
        multi = group
    if multi is not None:
        if ratio:
            return None
        forced.pop(multi[0], None)
        quiet.pop(multi[0], None)
    return Retry(forced, multi, q, quiet)
