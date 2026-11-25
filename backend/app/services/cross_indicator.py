import re
from dataclasses import dataclass

from app.core.stats_db import StatsNotLoadedError
from app.services import analysis, catalog, indicators
from app.services.josa import josa, ro

COUNT_UNITS = {"명", "건", "세대", "가구"}
OFFICIAL = {
    ("총인구", "세대수", 1): "세대당 인구",
    ("출생아 수", "총인구", 1000): "조출생률",
    ("사망자 수", "총인구", 1000): "조사망률",
    ("혼인 건수", "총인구", 1000): "조혼인율",
    ("이혼 건수", "총인구", 1000): "조이혼율",
    ("이혼 건수", "혼인 건수", 100): "혼인 대비 이혼 건수 비율",
    ("등록외국인 수", "총인구", 100): "등록외국인 비율",
    ("다문화 출생아 수", "출생아 수", 100): "다문화 출생 비중",
    ("다문화 혼인 건수", "혼인 건수", 100): "다문화 혼인 비중",
    ("미혼 인구", "총인구", 100): "미혼 비율",
}
KINDS = ("value", "compare", "trend", "rank")
DIGITS = 2

PER = re.compile(r"^(?P<n>[\d,]+|천|만|백|십)?(?:명|세대|건|가구)?당$")
VERSUS = re.compile(r"^(?:의|에)?대비$")
WITHIN = re.compile(r"^(?:중|가운데|중에서|에서)$")
RATE_AFTER = re.compile(r"^(?:의)?(?:비율|비중|퍼센트|몇퍼센트|몇프로|%)")
NUMBERS = {"천": 1000, "만": 10000, "백": 100, "십": 10}
RATE_ANYWHERE = re.compile(r"비율|비중|퍼센트|몇프로|%")
LEADING_NUMBER = re.compile(r"([\d,]+|천|만|백)$")
BARE_HOUSEHOLD = re.compile(r"세대(?!\s*수)(?=\s*(?:[\d,]+|천|만|백)?\s*(?:세대)?\s*당)")


AFTER_VERSUS = re.compile(r"^(?:에|의)?대비")
DIVIDE_MARKER = re.compile(r"^(?:으)?로$")
DIVIDE_AFTER = re.compile(r"^(?:으)?로나(?:누|눈|눠|눌|눴)")
DIVIDE_NUMERATOR = re.compile(r"^(?:을|를)?나(?:누|눈|눠|눌|눴)")
OBJECT = re.compile(r"^(?:을|를)?$")
AND_BETWEEN = re.compile(r"^(?:와|과|하고|랑|이랑|및|,|그리고)$")
SUM_TAIL = re.compile(r"^(?:의|을|를)?(?:합계|합친것|합친|합해서|합하면|합|더한것|더한|더하면)")
DEN_LABEL = {"총인구": "인구", "세대수": "세대"}


@dataclass
class Cross:
    num: str
    den: str
    scale: int
    form: str = "per"
    official: str | None = None
    dynamic: str | None = None
    units: dict | None = None
    plus: str | None = None

    @property
    def verb(self) -> str:
        return {"diff": "뺄", "sum": "더할"}.get(self.form, "나눌")

    @property
    def num_names(self) -> list[str]:
        return [self.num, self.plus] if self.plus else [self.num]

    @property
    def num_label(self) -> str:
        return f"{josa(self.num, '과와')} {self.plus}" if self.plus else self.num

    def unit_of(self, name: str) -> str:
        return (self.units or {}).get(name) or _unit(name)

    @property
    def name(self) -> str:
        den, num = DEN_LABEL.get(self.den, self.den), self.num_label
        if self.form == "diff":
            return f"{josa(num, '과와')} {self.den}의 차이"
        if self.form == "sum":
            return f"{josa(num, '과와')} {self.den}의 합"
        if self.form == "within":
            return f"{self.den} 중 {num} 비율"
        if self.form == "versus":
            return f"{self.den} 대비 {num}" + (" 비율" if self.scale == 100 else "")
        if self.scale == 1:
            return f"{den}당 {num}"
        return f"{den} {self.scale:,}{self.unit_of(self.den)}당 {num}"

    @property
    def unit(self) -> str:
        if self.form in ("diff", "sum"):
            return self.unit_of(self.num)
        if self.scale == 100:
            return "%"
        if self.form == "versus":
            return "배"
        return self.unit_of(self.num)

    @property
    def formula(self) -> str:
        times = f" × {self.scale:,}" if self.scale != 1 else ""
        num = f"({self.num} + {self.plus})" if self.plus else self.num
        if self.form == "diff":
            return f"{num} − {self.den}"
        if self.form == "sum":
            return f"{num} + {self.den}"
        return f"{num} ÷ {self.den}{times}"

    def as_dict(self) -> dict:
        out = {"num": self.num, "den": self.den, "scale": self.scale, "form": self.form}
        if self.dynamic:
            out["dynamic"] = self.dynamic
        if self.units:
            out["units"] = self.units
        if self.plus:
            out["plus"] = self.plus
        return out

    @property
    def unknown(self) -> str | None:
        return {"num": self.num, "den": self.den}.get(self.dynamic or "")


def _unit(name: str) -> str:
    return (indicators.spec(name) or {}).get("unit") or ""


def _countable(name: str) -> bool:
    sp = indicators.spec(name) or {}
    return sp.get("unit") in COUNT_UNITS and sp.get("kind") != "composition"


BARE_PER_PEOPLE = re.compile(r"(?<![가-힣\d,])((?:[\d,]+|천|만)\s*명\s*당)")


def _bare_per_people(question: str) -> str:
    m = BARE_PER_PEOPLE.search(question)
    if not m or indicators.find_in_text(question[: m.start()]):
        return question
    return f"{question[: m.start()]}인구 {question[m.start() :]}"


SUBJECT = re.compile(r"^(?:은|는|이|가)$")
DIFF_AFTER = re.compile(r"^(?:의)?(?:차이|격차)")
MORE_AFTER = re.compile(r"^보다\S*?몇\S*?(?:많|적|높|낮|더)")
MINUS = (re.compile(r"^에서$"), re.compile(r"^(?:을|를)?(?:빼|뺀)"))
TIMES_AFTER = re.compile(r"^의?몇배")
INCLUDE_AFTER = re.compile(r"^(?:도|까지|을|를|와|과)?(?:포함|합쳐|합친|합하면|더해|더하면)")
OFFICIAL_DIFF = {("출생아 수", "사망자 수"): "자연증가 건수"}


def _diff_or_times(a: str, b: str, between: str, after: str, plus) -> "Cross | None":
    if plus:
        return None
    if SUBJECT.match(between) and TIMES_AFTER.match(after):
        return Cross(num=a, den=b, scale=1, form="versus")
    if INCLUDE_AFTER.match(after) and _unit(a) == _unit(b):
        return Cross(num=a, den=b, scale=1, form="sum")
    diff = (
        (AND_BETWEEN.match(between) and DIFF_AFTER.match(after))
        or (SUBJECT.match(between) and MORE_AFTER.match(after))
        or (MINUS[0].match(between) and MINUS[1].match(after))
    )
    if not diff or _unit(a) != _unit(b):
        return None
    return Cross(num=a, den=b, scale=1, form="diff", official=OFFICIAL_DIFF.get((a, b)))


def detect(question: str) -> Cross | None:
    question = BARE_HOUSEHOLD.sub("세대 수", question)
    question = _bare_per_people(question)
    spans = sorted(indicators.find_in_text(question) or [])
    names = list(dict.fromkeys(name for _, _, name in spans))
    text = indicators._norm(question)
    plus = None
    if len(spans) == 3 and len(names) == 3:
        (_, b_end, b_name), (c_start, c_end, c_name) = spans[1], spans[2]
        if not AND_BETWEEN.match(text[b_end:c_start]) or _unit(c_name) != _unit(b_name):
            return None
        plus, spans = c_name, spans[:2]
        text = text[:b_end] + SUM_TAIL.sub("", text[c_end:])
    elif len(spans) != 2 or len(names) != 2:
        return None
    (a_start, a_end, a), (b_start, b_end, b) = spans
    if not (_countable(a) and _countable(b)) or (plus and not _countable(plus)):
        return None
    between, after = text[a_end:b_start], text[b_end:]
    if OBJECT.fullmatch(between) and DIVIDE_AFTER.match(after) and not plus:
        return _division(a, b, after)
    if DIVIDE_MARKER.fullmatch(between) and DIVIDE_NUMERATOR.match(after) and not plus:
        return _division(b, a, after)
    if found := _diff_or_times(a, b, between, after, plus):
        return found
    if not between and not plus and (m := AFTER_VERSUS.match(after)):
        scale = 100 if RATE_ANYWHERE.search(after[m.end() :]) else 1
        official = OFFICIAL.get((a, b, scale))
        return Cross(num=a, den=b, scale=scale, form="versus", official=official)
    form = "per"
    if m := PER.match(between):
        n = m.group("n")
        if not n and between == "당" and (pre := LEADING_NUMBER.search(text[:a_start])):
            n = pre.group(1)
        scale = NUMBERS.get(n) if n in NUMBERS else int(n.replace(",", "")) if n else 1
        if scale <= 0:
            return None
    elif VERSUS.match(between):
        rate = RATE_AFTER.match(after) or a == "총인구"
        scale, form = (100 if rate else 1), "versus"
    elif WITHIN.match(between) and RATE_AFTER.match(after):
        scale, form = 100, "within"
    else:
        return None
    if plus:
        return Cross(num=b, den=a, scale=scale, form=form, plus=plus)
    return Cross(num=b, den=a, scale=scale, form=form, official=OFFICIAL.get((b, a, scale)))


def _division(num: str, den: str, after: str) -> Cross:
    scale = 100 if RATE_ANYWHERE.search(after) else 1
    form = "versus" if scale == 100 or _unit(num) == _unit(den) else "per"
    return Cross(num=num, den=den, scale=scale, form=form)


MARKER = re.compile(r"^(?P<stem>.*?)(?P<n>[\d,]+|천|만|백)?(?P<unit>명|세대|가구|건)?당$")
VERSUS_TOKEN = re.compile(r"^(?P<stem>.*?)(?:의|에)?대비$")
WITHIN_TOKENS = {"중", "중에", "가운데", "중에서"}
ENDING = re.compile(
    r"(?:은|는|이|가|을|를|의)?\s*(?:몇|얼마|어떻게|어때|알려|인가요|이에요|예요|입니까|일까|\?).*$"
)
JOSA_END = re.compile(r"(?:은|는|이|가|을|를|의)$")
YEAR_TOKEN = re.compile(r"^(?:\d{2,4}년(?:도)?|올해|작년|지난해|금년|최근|현재)(?:의|에|기준)?$")
PLAIN_WORDS = {"남자", "여자", "남성", "여성", "총", "전체", "주민등록"}
PEOPLE_WORDS = {"사람", "사람들", "주민", "주민들", "인구"}
COUNT_PHRASE = re.compile(r"(?:수|인구|가구|건수|건|사람|세대)$")


def _number(n: str | None) -> int:
    if not n:
        return 1
    return NUMBERS.get(n) or int(n.replace(",", ""))


def _left_phrase(tokens: list[str]) -> str:
    out: list[str] = []
    for t in reversed(tokens):
        bare = JOSA_END.sub("", t)
        if YEAR_TOKEN.match(t) or (bare and indicators.find_regions(bare)):
            break
        out.insert(0, t)
    return JOSA_END.sub("", " ".join(out)).strip()


def _right_phrase(tokens: list[str]) -> str:
    return JOSA_END.sub("", ENDING.sub("", " ".join(tokens)).strip()).strip()


def _names_ratio(phrase: str) -> bool:
    """말 전체가 사전의 비율 지표 이름인가 ("혼자 사는 집 비율" → 1인 가구 비율).
    'A 중 B 비율'에서 A가 사전 지표가 아니고 B 비율이 비율 지표면 B를 떼어 A로 나누지 않는다:
    "전체 가구 중 혼자 사는 집 비율"을 1인 가구 수 ÷ 가구(총가구, 집단가구 포함)로 나눠 지표 정의와
    다른 값을 냈다 (11/25). A가 사전 지표면("인구 중 외국인주민 비율") 말한 분모로 나눈다"""
    spans = indicators.find_in_text(phrase) or []
    if len(spans) != 1:
        return False
    start, end, name = spans[0]
    text = indicators._norm(phrase)
    rest = re.sub(r"의", "", text[:start] + text[end:])
    return (not rest or rest in PLAIN_WORDS) and (indicators.spec(name) or {}).get("unit") == "%"


def _side(phrase: str) -> tuple[str | None, str | None]:
    if not phrase:
        return None, None
    if indicators._norm(phrase) in PEOPLE_WORDS:
        return "총인구", None
    spans = indicators.find_in_text(phrase) or []
    if len(spans) > 1:
        return None, None
    if spans:
        start, end, name = spans[0]
        text = indicators._norm(phrase)
        rest = re.sub(r"(수|의|명)", "", text[:start] + text[end:])
        if not rest or rest in PLAIN_WORDS:
            return (name, None) if _countable(name) else (None, None)
    if not COUNT_PHRASE.search(phrase) or len(indicators._norm(phrase)) < 2:
        return None, None
    return None, phrase


def detect_partial(question: str, both: bool = False) -> Cross | None:
    if detect(question) is not None:
        return None
    tokens = BARE_HOUSEHOLD.sub("세대 수", question).replace("?", " ").split()
    for i, t in enumerate(tokens):
        left, right = tokens[:i], tokens[i + 1 :]
        if (m := MARKER.match(t)) and t != "당" or t == "당":
            stem, n = (m["stem"], m["n"]) if m else ("", None)
            if stem:
                left = [*left, stem]
            if not n and left and (left[-1] in NUMBERS or left[-1].replace(",", "").isdigit()):
                n = left.pop()
            form, scale = "per", _number(n)
        elif m := VERSUS_TOKEN.match(t):
            if m["stem"]:
                left = [*left, m["stem"]]
            rate = bool(RATE_ANYWHERE.search(indicators._norm(" ".join(right))))
            form, scale = "versus", 100 if rate else 1
        elif t in WITHIN_TOKENS:
            text = indicators._norm(" ".join(right))
            if not RATE_ANYWHERE.search(text):
                continue
            form, scale = "within", 100
        else:
            continue
        right_phrase = _right_phrase(right)
        den_name, den_new = _side(_left_phrase(left))
        if form != "per":
            if not den_name and _names_ratio(right_phrase):
                # "전체 가구 중 1인 가구 비율": 분모가 사전 지표가 아니면 비율 지표 자체를 물은 것.
                # 분모를 사전 지표로 말하면("인구 중 외국인주민 비율") 그 분모로 나눈다
                return None
            right_phrase = re.sub(r"\s*(?:의\s*)?(?:비율|비중)$", "", right_phrase).strip()
        num_name, num_new = _side(right_phrase)
        if den_name and num_new:
            return Cross(num=num_new, den=den_name, scale=scale, form=form, dynamic="num")
        if num_name and den_new:
            return Cross(num=num_name, den=den_new, scale=scale, form=form, dynamic="den")
        if num_name and den_name and num_name != den_name:
            official = OFFICIAL.get((num_name, den_name, scale))
            return Cross(num=num_name, den=den_name, scale=scale, form=form, official=official)
        if both and num_new and den_new:
            return Cross(num=num_new, den=den_new, scale=scale, form=form, dynamic="both")
        return None
    return None


def _fail(c: Cross, reason: str) -> dict:
    return {"status": "missing_data", "indicator": c.name, "reason": reason, "guide": ""}


def _round(v):
    if v and abs(v) < 1:
        return float(f"{v:.3g}")
    return round(v, DIGITS)


def _div(a, b, scale):
    if a is None or b in (None, 0):
        return None
    return _round(a / b * scale)


def _calc_raw(c: Cross, a, b):
    if a is None or b is None:
        return None
    if c.form == "diff":
        return a - b
    if c.form == "sum":
        return a + b
    return None if b == 0 else a / b * c.scale


def _calc(c: Cross, a, b):
    v = _calc_raw(c, a, b)
    return None if v is None else _round(v)


def compute(q: dict, run) -> dict:
    c = Cross(**q["cross"])
    kind = q.get("type")
    if kind not in KINDS:
        return _fail(c, "두 지표를 나눈 값은 값 · 비교 · 추이 · 순위로만 답할 수 있습니다.")
    if q.get("ages"):
        return _fail(c, "두 지표를 나눈 값은 연령 구간을 나눠 계산하지 않습니다.")
    base = {**q, "cross": None, "indicators": None}
    if kind in ("value", "trend") and not q.get("regions"):
        base["regions"] = ["전국"]
    if kind == "rank":
        base["top"] = None

    names = [*c.num_names, c.den]

    def both(extra: dict | None = None):
        extra = extra or {}
        return [run({**base, **extra, "indicator": name, "term": name}) for name in names]

    parts = both()
    if (
        kind in ("value", "rank", "compare")
        and all(p.get("status") == "ok" for p in parts)
        and (not q.get("year"))
    ):
        years = {p.get("year") for p in parts}
        if len({(p.get("year"), p.get("as_of")) for p in parts}) > 1:
            year = min(y for y in years if y is not None)
            if any(p.get("as_of") for p in parts):
                year = min(year, indicators._data_years()[1])
            asked = {**(q.get("asked") or {}), "latest": False}
            parts = both({"year": year, "asked": asked})
    for name, p in zip(names, parts, strict=True):
        if p.get("status") != "ok":
            out = dict(p)
            reason = out.get("reason") or ""
            out["reason"] = (
                f"{josa(c.name, '을를')} 계산하려면 {josa(name, '이가')} 필요합니다. {reason}"
            )
            out["reason"] = out["reason"].strip()
            return out
    *nums, den = parts
    return combine_parts(c, nums, den, q)


def combine_parts(c: Cross, nums: list[dict], den: dict, q: dict) -> dict:
    kind = q.get("type")
    if len(nums) > 1:
        if why := _mismatch(nums[0], nums[1], q):
            return _fail(
                c, f"{josa(c.num, '과와')} {josa(c.plus, '을를')} 더할 수 없습니다: {why}."
            )
        num = _sum_parts(nums, kind)
        if num is None:
            return _fail(c, f"{josa(c.num, '과와')} {josa(c.plus, '을를')} 더할 값이 없습니다.")
    else:
        num = nums[0]
    if kind == "value":
        return combine_value(c, num, den, q)
    if why := _mismatch(num, den, q) or _part_of_whole(c, num, den):
        return _fail(
            c, f"{josa(c.num_label, '과와')} {josa(c.den, '을를')} {c.verb} 수 없습니다: {why}."
        )
    if gap := _diff_basis(c, num, den):
        return _side_by_side(num, den, gap)
    out = _shell(c, num, den)
    if kind == "trend":
        dv = {s["year"]: s.get("value") for s in den.get("series") or []}
        raw = [
            (s["year"], _calc_raw(c, s.get("value"), dv[s["year"]]))
            for s in num.get("series") or []
            if s["year"] in dv
        ]
        raw = [(y, v) for y, v in raw if v is not None]
        series = [{"year": y, "value": _round(v)} for y, v in raw]
        if len(series) < 2:
            return _fail(
                c,
                f"{josa(c.num_label, '과와')} {josa(c.den, '이가')} "
                "함께 있는 연도가 두 해 이상 없습니다.",
            )
        out.update(region=num.get("region"), sex=num.get("sex"), series=series)
        out["change"] = _change(series, c, raw=[v for _, v in raw])
    else:
        key = "values" if kind == "compare" else "ranking"
        dv = {v["region"]: v.get("value") for v in den.get(key) or []}
        raw = {
            v["region"]: _calc_raw(c, v.get("value"), dv.get(v["region"]))
            for v in num.get(key) or []
        }
        vals = [{"region": r, "value": None if v is None else _round(v)} for r, v in raw.items()]
        if any(v["value"] is None for v in vals) or not vals:
            return _fail(c, "어떤 지역은 두 값 중 하나가 없습니다.")
        out.update(year=num.get("year"), sex=num.get("sex"))
        if kind == "compare":
            out.update(_compare_facts(vals, out["unit"], raw=raw))
        else:
            out.update(_rank_facts(vals, num, q, raw=raw))
    return out


def combine_value(c: Cross, num: dict, den: dict, q: dict) -> dict:
    if why := _mismatch(num, den, {**q, "type": "value"}) or _part_of_whole(c, num, den):
        return _fail(
            c, f"{josa(c.num_label, '과와')} {josa(c.den, '을를')} {c.verb} 수 없습니다: {why}."
        )
    if gap := _diff_basis(c, num, den):
        return _side_by_side(num, den, gap)
    out = _shell(c, num, den)
    out.update(region=num.get("region"), year=num.get("year"), sex=num.get("sex"))
    out["value"] = _calc(c, num.get("value"), den.get("value"))
    out["operands"] = [
        {"label": c.num_label, "value": num.get("value"), "unit": c.unit_of(c.num)},
        {"label": c.den, "value": den.get("value"), "unit": c.unit_of(c.den)},
    ]
    if c.form in ("diff", "sum"):
        out["op"] = "−" if c.form == "diff" else "+"
    elif c.scale != 100:
        out["scale"] = c.scale
    return out


def _change(series: list[dict], c: Cross, raw: list[float] | None = None) -> dict:
    first, last = (raw[0], raw[-1]) if raw else (series[0]["value"], series[-1]["value"])
    if c.scale == 100:
        diff = analysis.point_change(first, last)
    else:
        diff = analysis.change(first, last)
    out = {
        "from_year": series[0]["year"],
        "to_year": series[-1]["year"],
        "diff": _round(diff),
        "diff_unit": "%p" if c.scale == 100 else c.unit,
    }
    if first:
        key = "relative_rate" if c.scale == 100 else "rate"
        out[key] = round(analysis.growth_rate(first, last), 1 if key == "relative_rate" else 2)
        if key == "rate":
            out["rate_unit"] = "%"
    years = series[-1]["year"] - series[0]["year"]
    if "rate" in out and years and first > 0 and last > 0:
        out["cagr"] = round(((last / first) ** (1 / years) - 1) * 100, 1)
    return out


def _part_of_whole(c: Cross, num: dict, den: dict) -> str | None:
    if c.form != "within":
        return None
    if num.get("unit") and den.get("unit") and num.get("unit") != den.get("unit"):
        return f"단위가 달라({num.get('unit')}, {den.get('unit')}) 한쪽이 다른 쪽의 일부가 아닙니다"

    def values(r: dict) -> list:
        if "value" in r:
            return [r.get("value")]
        rows = r.get("series") or r.get("values") or r.get("ranking") or []
        return [row.get("value") for row in rows]

    if any(
        a is not None and b is not None and a > b
        for a, b in zip(values(num), values(den), strict=False)
    ):
        return f"{c.num_label}이(가) {c.den}보다 커서 그 일부가 아닙니다"
    return None


def basis_gap(a: dict, b: dict, label_a: str, label_b: str) -> str | None:
    try:
        got = catalog.comparable(a.get("tbl_id"), b.get("tbl_id"))
    except StatsNotLoadedError:
        return None
    if got["comparable"] is not False:
        return None
    sa, sb = got["surveys"]
    return f"{josa(label_a, '은는')} {sa}, {josa(label_b, '은는')} {sb} 통계입니다. {got['reason']}"


def _diff_basis(c: Cross, num: dict, den: dict) -> str | None:
    return basis_gap(num, den, c.num_label, c.den) if c.form == "diff" else None


def _side_by_side(num: dict, den: dict, gap: str) -> dict:
    last = {**den, "notes": [*(den.get("notes") or []), gap]}
    return {
        **{k: num.get(k) for k in ("source", "basis", "tbl_id", "url")},
        "status": "ok",
        "split": [num, last],
        "split_by": "indicator",
        "basis_gap": gap,
    }


def _mismatch(num: dict, den: dict, q: dict) -> str | None:
    for p in (num, den):
        if p.get("scenario") or "추계" in str(p.get("basis") or ""):
            return "장래 추계 값은 나누지 않습니다"
    sex = q.get("sex") or "계"
    if any((p.get("sex") or "계") != sex for p in (num, den)):
        return f"{josa(_name(num, den, sex), '은는')} 성별로 나뉘어 있지 않습니다"
    if bool(num.get("as_of")) != bool(den.get("as_of")):
        return "한쪽은 최신 달 값이라 연간 값과 섞을 수 없습니다"
    if q.get("type") == "value":
        if num.get("year") != den.get("year"):
            return f"두 값의 연도가 다릅니다 ({num.get('year')}년, {den.get('year')}년)"
        if num.get("region") != den.get("region"):
            return "두 값의 지역이 다릅니다"
    if q.get("type") in ("compare", "rank"):
        if num.get("year") != den.get("year"):
            return f"두 값의 연도가 다릅니다 ({num.get('year')}년, {den.get('year')}년)"
        key = "values" if q.get("type") == "compare" else "ranking"
        if {v["region"] for v in num.get(key) or []} != {v["region"] for v in den.get(key) or []}:
            return "두 값의 지역 목록이 다릅니다 (지역 단위가 다른 통계)"
    return None


def _name(num: dict, den: dict, sex: str) -> str:
    return next(p["indicator"] for p in (num, den) if (p.get("sex") or "계") != sex)


def _shell(c: Cross, num: dict, den: dict) -> dict:
    extra = [
        {k: den.get(k) for k in ("source", "basis", "tbl_id", "url")},
        *(num.get("extra_sources") or []),
        *(den.get("extra_sources") or []),
    ]
    notes = [
        f"지표 사전에 없는 값이라 {ro(c.formula)} 계산했습니다",
        f"두 통계의 기준: {josa(c.num_label, '은는')} {num.get('basis')}, "
        f"{josa(c.den, '은는')} {den.get('basis')}",
        *_reference_note(c, num, den),
        *(num.get("notes") or []),
        *(n for n in den.get("notes") or [] if n not in (num.get("notes") or [])),
    ]
    return {
        "status": "ok",
        "indicator": c.name,
        "kind": "ratio" if c.scale == 100 else "value",
        "unit": c.unit,
        "diff_unit": "%p" if c.scale == 100 else c.unit,
        "digits": DIGITS,
        "formula": c.formula,
        "source": num.get("source"),
        "basis": num.get("basis"),
        "tbl_id": num.get("tbl_id"),
        "url": num.get("url"),
        "extra_sources": [
            e for e in extra if e.get("source") and e.get("tbl_id") != num.get("tbl_id")
        ],
        "notes": notes,
        "method": "cross",
        "cross": c.as_dict(),
    }


POINT_DATE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일(?!\s*~)(?:\s*기준)?\s*(?:\(|$|,)")


def _point_date(basis) -> str | None:
    text = str(basis or "")
    if "~" in text:
        return None
    m = POINT_DATE.search(text)
    return f"{int(m[1])}월 {int(m[2])}일" if m else None


def _reference_note(c: Cross, num: dict, den: dict) -> list[str]:
    a, b = _point_date(num.get("basis")), _point_date(den.get("basis"))
    if a and b and a != b:
        return [
            f"두 값의 기준 시점이 다릅니다 ({c.num_label} {a}, {c.den} {b}). "
            "같은 날의 값이 아니라 비율은 근사값입니다"
        ]
    return []


def _sum_parts(parts: list[dict], kind: str) -> dict | None:
    a, b = parts

    def add(x, y):
        return None if x is None or y is None else x + y

    out = {
        **a,
        "indicator": f"{a.get('indicator')} + {b.get('indicator')}",
        "basis": a.get("basis")
        if a.get("basis") == b.get("basis")
        else f"{a.get('basis')} / {b.get('basis')}",
        "notes": [
            *(a.get("notes") or []),
            *(n for n in b.get("notes") or [] if n not in (a.get("notes") or [])),
        ],
        "extra_sources": [
            *(a.get("extra_sources") or []),
            *(
                [{k: b.get(k) for k in ("source", "basis", "tbl_id", "url")}]
                if b.get("tbl_id") != a.get("tbl_id")
                else []
            ),
            *(b.get("extra_sources") or []),
        ],
    }
    if kind == "value":
        out["value"] = add(a.get("value"), b.get("value"))
        return out if out["value"] is not None else None
    if kind == "trend":
        bv = {s["year"]: s.get("value") for s in b.get("series") or []}
        series = [
            {"year": s["year"], "value": add(s.get("value"), bv[s["year"]])}
            for s in a.get("series") or []
            if s["year"] in bv
        ]
        out["series"] = [s for s in series if s["value"] is not None]
        return out if out["series"] else None
    key = "values" if kind == "compare" else "ranking"
    bv = {v["region"]: v.get("value") for v in b.get(key) or []}
    rows = [
        {"region": v["region"], "value": add(v.get("value"), bv.get(v["region"]))}
        for v in a.get(key) or []
    ]
    out[key] = [r for r in rows if r["value"] is not None]
    return out if out[key] else None


def _compare_facts(vals: list[dict], unit: str, raw: dict | None = None) -> dict:

    def exact(v):
        return raw[v["region"]] if raw and raw.get(v["region"]) is not None else v["value"]

    vals = sorted(vals, key=exact, reverse=True)
    hi, lo = vals[0], vals[-1]
    out = {
        "values": vals,
        "highest": hi["region"],
        "lowest": lo["region"],
        "gap": _round(exact(hi) - exact(lo)),
        "gap_unit": "%p" if unit == "%" else unit,
    }
    if len(vals) == 2 and exact(vals[1]):
        a, b = vals
        out["ratio"] = {
            "a": a["region"],
            "b": b["region"],
            "value": round(exact(a) / exact(b), 2),
        }
    return out


def _rank_facts(vals: list[dict], num: dict, q: dict, raw: dict | None = None) -> dict:

    def exact(v):
        return raw[v["region"]] if raw and raw.get(v["region"]) is not None else v["value"]

    low_first = "낮은" in str(num.get("order") or "")
    ranked = sorted(vals, key=exact, reverse=not low_first)
    top = q.get("top") or len(ranked)
    return {
        "level": num.get("level"),
        "order": num.get("order"),
        "total_regions": len(ranked),
        "ranking": [{**v, "rank": i} for i, v in enumerate(ranked[:top], start=1)],
    }
