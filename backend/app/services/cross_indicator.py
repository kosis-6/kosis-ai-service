"""두 지표를 나눈 값: 지표 사전의 두 지표(건수·인원)를 각각 계산해 나눈다 (동적 지표 3단계)

  "세대당 출생아 수"              출생아 수 ÷ 세대수
  "인구 1,000명당 외국인근로자 수" 외국인근로자 수 ÷ 총인구 × 1,000
  "출생아 수 대비 사망자 수"       사망자 수 ÷ 출생아 수 (배). "비율"이면 × 100 (%)
  "혼인 건수 중 다문화 혼인 비율"   다문화 혼인 건수 ÷ 혼인 건수 × 100

- 재료는 건수·인원만 쓴다 (비율·지수를 다시 나누지 않는다). 둘 다 지표 사전에 있으면 통계표
  찾기·HCX를 더 부르지 않는다. 숫자는 지금 계산 엔진(qa.compute)이 DB 값으로 낸다
- 한쪽만 사전에 있으면 (detect_partial, "인구 1,000명당 1인 가구 수") 사전에 없는 쪽은 동적 지표
  경로(계산 방법 캐시 → 통계표 찾기 · 칸 고르기)로 값을 구한다 (ask_session._cross_dynamic).
  통계표 찾기가 LLM을 부르므로 값 질문(한 지역 · 한 해)만 한다
- 공식 지표가 있는 조합은 그 지표로 답한다 (예: 인구 1,000명당 혼인 건수 → 조혼인율. 분모가
  주민등록인구가 아니라 연앙인구라 직접 나누면 공표 값과 다르다)
- 나누기 전에 확인한다: 두 값의 지역 · 연도 · 성별이 같은가, 추계 값이 아닌가.
  하나라도 맞지 않으면 숫자를 내지 않는다
"""

import re
from dataclasses import dataclass

from app.services import analysis, indicators
from app.services.josa import josa, ro

# 재료로 쓸 수 있는 지표의 단위 (건수·인원). 비율·지수·나이는 나누지 않는다
COUNT_UNITS = {"명", "건", "세대", "가구"}
# 공식 지표가 있는 조합: (분자, 분모, 배율) → 지표 사전의 이름
OFFICIAL = {
    ("총인구", "세대수", 1): "세대당 인구",
    ("출생아 수", "총인구", 1000): "조출생률",
    ("사망자 수", "총인구", 1000): "조사망률",
    ("혼인 건수", "총인구", 1000): "조혼인율",
    ("이혼 건수", "총인구", 1000): "조이혼율",
    ("이혼 건수", "혼인 건수", 100): "혼인 대비 이혼 건수 비율",
    ("등록외국인 수", "총인구", 100): "등록외국인 비율",
    ("외국인주민 수", "총인구", 100): "외국인주민 비율",
    ("다문화 출생아 수", "출생아 수", 100): "다문화 출생 비중",
    ("다문화 혼인 건수", "혼인 건수", 100): "다문화 혼인 비중",
    # 공식 미혼 비율의 분모는 15세 이상 인구라 총인구로 나누면 다르다
    ("미혼 인구", "총인구", 100): "미혼 비율",
}
KINDS = ("value", "compare", "trend", "rank")
DIGITS = 2

# 두 지표 이름 사이의 말 (공백을 뺀 문장 기준)
PER = re.compile(
    r"^(?P<n>[\d,]+|천|만|백|십)?(?:명|세대|건|가구)?당$"
)  # "당", "1,000명당", "천명당"
VERSUS = re.compile(r"^(?:의|에)?대비$")  # "대비", "의 대비", "에 대비"
WITHIN = re.compile(r"^(?:중|가운데|중에서|에서)$")  # "중", "가운데"
RATE_AFTER = re.compile(r"^(?:의)?(?:비율|비중|퍼센트|몇퍼센트|몇프로|%)")
NUMBERS = {"천": 1000, "만": 10000, "백": 100, "십": 10}
# '당·대비·중' 뒤 어딘가에 비율을 묻는 말
# (detect_partial: "중 통근하는 사람 비율"의 비율은 끝에 온다)
RATE_ANYWHERE = re.compile(r"비율|비중|퍼센트|몇프로|%")
LEADING_NUMBER = re.compile(r"([\d,]+|천|만|백)$")
# "세대당"의 "세대"는 세대수 지표의 이름·별칭이 아니라서, 찾을 때만 "세대 수"로 읽는다
BARE_HOUSEHOLD = re.compile(r"세대(?!\s*수)(?=\s*(?:[\d,]+|천|만|백)?\s*(?:세대)?\s*당)")


AFTER_VERSUS = re.compile(r"^(?:에|의)?대비")
# 분자 두 지표 사이의 말 ("혼인 건수와 이혼 건수"), 그 뒤의 합 표현 ("합", "합친", "더한")
AND_BETWEEN = re.compile(r"^(?:와|과|하고|랑|이랑|및|,|그리고)$")
SUM_TAIL = re.compile(r"^(?:의|을|를)?(?:합계|합친것|합친|합해서|합하면|합|더한것|더한|더하면)")
# "A당 B"에서 분모를 부르는 말 (총인구 → "인구 1,000명당", 세대수 → "세대당")
DEN_LABEL = {"총인구": "인구", "세대수": "세대"}


@dataclass
class Cross:
    num: str  # 분자 지표 (사전 이름)
    den: str  # 분모 지표
    scale: int  # 1: 그대로, 100: %, 1000: N명당
    form: str = "per"  # per: A당 B / versus: A 대비 B / within: A 중 B 비율 / diff: A − B
    official: str | None = None  # 공식 지표가 있으면 그 이름
    dynamic: str | None = None  # 사전에 없는 쪽 ("num" · "den"). 그 쪽은 동적 지표로 구한다
    units: dict | None = None  # 사전에 없는 쪽의 단위 (계산한 뒤 채운다)
    plus: str | None = None  # 분자를 두 지표의 합으로 ("혼인 건수와 이혼 건수"). 표는 3개까지

    @property
    def verb(self) -> str:
        """계산할 수 없을 때의 말 ("뺄 수 없습니다" · "나눌 수 없습니다")"""
        return "뺄" if self.form == "diff" else "나눌"

    @property
    def num_names(self) -> list[str]:
        """분자에 더할 지표들"""
        return [self.num, self.plus] if self.plus else [self.num]

    @property
    def num_label(self) -> str:
        """분자를 부르는 말 ("혼인 건수와 이혼 건수")"""
        return f"{josa(self.num, '과와')} {self.plus}" if self.plus else self.num

    def unit_of(self, name: str) -> str:
        return (self.units or {}).get(name) or _unit(name)

    @property
    def name(self) -> str:
        den, num = DEN_LABEL.get(self.den, self.den), self.num_label
        if self.form == "diff":
            return f"{josa(num, '과와')} {self.den}의 차이"
        if self.form == "within":
            return f"{self.den} 중 {num} 비율"
        if self.form == "versus":
            return f"{self.den} 대비 {num}" + (" 비율" if self.scale == 100 else "")
        if self.scale == 1:
            return f"{den}당 {num}"
        return f"{den} {self.scale:,}{self.unit_of(self.den)}당 {num}"

    @property
    def unit(self) -> str:
        if self.form == "diff":
            return self.unit_of(self.num)
        if self.scale == 100:
            return "%"
        if self.form == "versus":
            return "배"
        return self.unit_of(self.num)  # 이름에 "인구 1,000명당"이 있어 단위는 분자의 단위만

    @property
    def formula(self) -> str:
        times = f" × {self.scale:,}" if self.scale != 1 else ""
        num = f"({self.num} + {self.plus})" if self.plus else self.num
        if self.form == "diff":
            return f"{num} − {self.den}"
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
        """사전에 없는 쪽의 말 ("1인 가구 수")"""
        return {"num": self.num, "den": self.den}.get(self.dynamic or "")


def _unit(name: str) -> str:
    return (indicators.spec(name) or {}).get("unit") or ""


def _countable(name: str) -> bool:
    sp = indicators.spec(name) or {}
    return sp.get("unit") in COUNT_UNITS and sp.get("kind") != "composition"


# 분모 없이 "1000명당 혼인 건수"라고만 하면 인구 1,000명당 (조혼인율 · 조출생률 등의 풀어 쓴 말)
BARE_PER_PEOPLE = re.compile(r"(?<![가-힣\d,])((?:[\d,]+|천|만)\s*명\s*당)")


def _bare_per_people(question: str) -> str:
    m = BARE_PER_PEOPLE.search(question)
    if not m or indicators.find_in_text(question[: m.start()]):
        return question  # 앞에 분모 지표가 있다 ("인구 1,000명당", "외국인주민 1,000명당")
    return f"{question[: m.start()]}인구 {question[m.start() :]}"


# 두 지표의 차이 · 몇 배 ("혼인 건수와 이혼 건수 차이", "혼인 건수가 이혼 건수보다 몇 건 많아",
# "출생아 수에서 사망자 수를 빼면", "혼인 건수는 이혼 건수의 몇 배"). 앞 지표가 A, 뒤 지표가 B
SUBJECT = re.compile(r"^(?:은|는|이|가)$")
DIFF_AFTER = re.compile(r"^(?:의)?(?:차이|격차)")
MORE_AFTER = re.compile(r"^보다\S*?몇\S*?(?:많|적|높|낮|더)")
MINUS = (re.compile(r"^에서$"), re.compile(r"^(?:을|를)?(?:빼|뺀)"))
TIMES_AFTER = re.compile(r"^의?몇배")
# 차이의 공식 지표 (A − B → 지표 사전 이름)
OFFICIAL_DIFF = {("출생아 수", "사망자 수"): "자연증가 건수"}


def _diff_or_times(a: str, b: str, between: str, after: str, plus) -> "Cross | None":
    if plus:
        return None
    if SUBJECT.match(between) and TIMES_AFTER.match(after):
        return Cross(num=a, den=b, scale=1, form="versus")  # A는 B의 몇 배 = A ÷ B
    diff = (
        (AND_BETWEEN.match(between) and DIFF_AFTER.match(after))
        or (SUBJECT.match(between) and MORE_AFTER.match(after))
        or (MINUS[0].match(between) and MINUS[1].match(after))
    )
    if not diff or _unit(a) != _unit(b):
        return None  # 단위가 다르면(건 − 명) 빼지 않는다
    return Cross(num=a, den=b, scale=1, form="diff", official=OFFICIAL_DIFF.get((a, b)))


def detect(question: str) -> Cross | None:
    """질문에서 두 지표를 나눈 표현을 찾는다. 없으면 None.
    질문에 사전 지표 이름이 정확히 두 개 있고, 그 사이 말이 '당·대비·중'일 때만 본다"""
    question = BARE_HOUSEHOLD.sub("세대 수", question)
    question = _bare_per_people(question)
    spans = sorted(indicators.find_in_text(question) or [])
    names = list(dict.fromkeys(name for _, _, name in spans))
    text = indicators._norm(question)
    plus = None
    if len(spans) == 3 and len(names) == 3:
        # "인구 1,000명당 혼인 건수와 이혼 건수 합": 분자가 두 지표의 합 (표 3개)
        (_, b_end, b_name), (c_start, c_end, c_name) = spans[1], spans[2]
        if not AND_BETWEEN.match(text[b_end:c_start]) or _unit(c_name) != _unit(b_name):
            return None
        # 세 번째 이름과 그 뒤의 합 표현을 지우고 두 지표처럼 읽는다 (뒤의 '비율' 등은 남긴다)
        plus, spans = c_name, spans[:2]
        text = text[:b_end] + SUM_TAIL.sub("", text[c_end:])
    elif len(spans) != 2 or len(names) != 2:
        return None
    (a_start, a_end, a), (b_start, b_end, b) = spans
    if not (_countable(a) and _countable(b)) or (plus and not _countable(plus)):
        return None
    between, after = text[a_end:b_start], text[b_end:]
    if found := _diff_or_times(a, b, between, after, plus):
        return found
    if not between and not plus and (m := AFTER_VERSUS.match(after)):
        # "이사한 사람 인구 대비 몇 퍼센트": A가 B 바로 앞에 붙고 B 뒤에 '대비' → A ÷ B
        scale = 100 if RATE_ANYWHERE.search(after[m.end() :]) else 1
        official = OFFICIAL.get((a, b, scale))
        return Cross(num=a, den=b, scale=scale, form="versus", official=official)
    form = "per"
    if m := PER.match(between):
        n = m.group("n")
        if not n and between == "당" and (pre := LEADING_NUMBER.search(text[:a_start])):
            n = pre.group(1)  # "1,000세대당": 숫자가 분모 이름 앞에
        scale = NUMBERS.get(n) if n in NUMBERS else int(n.replace(",", "")) if n else 1
        if scale <= 0:
            return None
    elif VERSUS.match(between):
        scale, form = (100 if RATE_AFTER.match(after) else 1), "versus"
    elif WITHIN.match(between) and RATE_AFTER.match(after):
        scale, form = 100, "within"
    else:
        return None
    if plus:
        return Cross(num=b, den=a, scale=scale, form=form, plus=plus)
    return Cross(num=b, den=a, scale=scale, form=form, official=OFFICIAL.get((b, a, scale)))


# ---- 한쪽만 사전에 있는 경우 ------------------------------------------------------------------

MARKER = re.compile(r"^(?P<stem>.*?)(?P<n>[\d,]+|천|만|백)?(?P<unit>명|세대|가구|건)?당$")
VERSUS_TOKEN = re.compile(r"^(?P<stem>.*?)(?:의|에)?대비$")
WITHIN_TOKENS = {"중", "가운데", "중에서"}
# 질문 끝의 묻는 말 ("1인 가구 수는 몇이야?" → "1인 가구 수")
ENDING = re.compile(
    r"(?:은|는|이|가|을|를|의)?\s*(?:몇|얼마|어떻게|어때|알려|인가요|이에요|예요|입니까|일까|\?).*$"
)
JOSA_END = re.compile(r"(?:은|는|이|가|을|를|의)$")
YEAR_TOKEN = re.compile(r"^(?:\d{2,4}년(?:도)?|올해|작년|지난해|금년|최근|현재)(?:의|에|기준)?$")
# 사전에 없는 쪽으로 볼 수 있는 말: 건수 · 인원 · 가구로 끝나야 한다 (값을 나눌 수 있는 것)
PLAIN_WORDS = {"남자", "여자", "남성", "여성", "총", "전체", "주민등록"}
COUNT_PHRASE = re.compile(r"(?:수|인구|가구|건수|건|사람|세대)$")


def _number(n: str | None) -> int:
    if not n:
        return 1
    return NUMBERS.get(n) or int(n.replace(",", ""))


def _left_phrase(tokens: list[str]) -> str:
    """'당·대비·중' 앞의 말: 앞쪽의 연도 · 지역 낱말은 뺀다 ("2020년 서울 인구" → "인구")"""
    out: list[str] = []
    for t in reversed(tokens):
        bare = JOSA_END.sub("", t)
        if YEAR_TOKEN.match(t) or (bare and indicators.find_regions(bare)):
            break
        out.insert(0, t)
    return JOSA_END.sub("", " ".join(out)).strip()


def _right_phrase(tokens: list[str]) -> str:
    return JOSA_END.sub("", ENDING.sub("", " ".join(tokens)).strip()).strip()


def _side(phrase: str) -> tuple[str | None, str | None]:
    """(사전 지표 이름, 사전에 없는 말). 둘 다 None이면 쓸 수 없는 말"""
    if not phrase:
        return None, None
    spans = indicators.find_in_text(phrase) or []
    if len(spans) > 1:
        return None, None
    if spans:
        start, end, name = spans[0]
        # 말 전체가 그 지표 이름이면 사전 지표 (성별 · '전체' 같은 말만 붙은 것도).
        # 다른 말이 붙었으면 다른 통계다 ("통근 인구"는 총인구가 아니다)
        text = indicators._norm(phrase)
        rest = re.sub(r"(수|의|명)", "", text[:start] + text[end:])
        if not rest or rest in PLAIN_WORDS:
            return (name, None) if _countable(name) else (None, None)
    if not COUNT_PHRASE.search(phrase) or len(indicators._norm(phrase)) < 2:
        return None, None
    return None, phrase


def detect_partial(question: str, both: bool = False) -> Cross | None:
    """'당 · 대비 · 중 비율'의 한쪽만 사전 지표인 질문 ("2020년 서울 인구 1,000명당 1인 가구 수").
    둘 다 사전 지표면 detect가, 둘 다 아니면 동적 지표(한 표 안의 계산)가 맡는다"""
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
                n = left.pop()  # "천 명당"
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
        if form != "per":
            right_phrase = re.sub(r"\s*(?:의\s*)?(?:비율|비중)$", "", right_phrase).strip()
        den_name, den_new = _side(_left_phrase(left))
        num_name, num_new = _side(right_phrase)
        if den_name and num_new:
            return Cross(num=num_new, den=den_name, scale=scale, form=form, dynamic="num")
        if num_name and den_new:
            return Cross(num=num_name, den=den_new, scale=scale, form=form, dynamic="den")
        if both and num_new and den_new:
            # 두 값 모두 사전에 없다 ("1인 가구 중 통근하는 사람 비율"): 질문 전체로
            # 한 표 안의 비율을 먼저 찾고, 안 되면 양쪽을 따로 찾아 나눈다 (ask_session에서만)
            return Cross(num=num_new, den=den_new, scale=scale, form=form, dynamic="both")
        return None
    return None


# ---- 계산 -----------------------------------------------------------------------------------


def _fail(c: Cross, reason: str) -> dict:
    """나누지 않는다. 숫자 없이 이유만 (qa._refusal_text가 reason을 그대로 쓴다)"""
    return {"status": "missing_data", "indicator": c.name, "reason": reason, "guide": ""}


def _round(v):
    """1보다 작으면 유효숫자 3자리까지 (세대당 출생아 0.0093명이 0.01로 뭉개지지 않게)"""
    if v and abs(v) < 1:
        return float(f"{v:.3g}")
    return round(v, DIGITS)


def _div(a, b, scale):
    if a is None or b in (None, 0):
        return None
    return _round(a / b * scale)


def _calc(c: Cross, a, b):
    """두 값으로 계산: 차이면 A − B, 그 밖은 A ÷ B × 배율"""
    if c.form == "diff":
        return None if a is None or b is None else _round(a - b)
    return _div(a, b, c.scale)


def compute(q: dict, run) -> dict:
    """두 지표를 각각 계산(run = qa.compute)해 나눈 결과. qa.compute 결과와 같은 모양"""
    c = Cross(**q["cross"])
    kind = q.get("type")
    if kind not in KINDS:
        return _fail(c, "두 지표를 나눈 값은 값 · 비교 · 추이 · 순위로만 답할 수 있습니다.")
    if q.get("ages"):
        return _fail(c, "두 지표를 나눈 값은 연령 구간을 나눠 계산하지 않습니다.")
    base = {**q, "cross": None, "indicators": None}
    if kind in ("value", "trend") and not q.get("regions"):
        # 지역을 말하지 않은 비율 · 차이는 전국 ("이사한 사람 인구 대비 %")
        base["regions"] = ["전국"]
    if kind == "rank":
        base["top"] = None  # 나눈 값으로 다시 순위를 매기므로 모든 지역을 받는다

    names = [*c.num_names, c.den]

    def both(extra: dict | None = None):
        extra = extra or {}
        return [run({**base, **extra, "indicator": name, "term": name}) for name in names]

    parts = both()
    if kind == "value" and all(p.get("status") == "ok" for p in parts) and not q.get("year"):
        years = {p.get("year") for p in parts}
        if len(years) > 1 or any(p.get("as_of") for p in parts):
            # 연도를 말하지 않아 두 지표가 각자 최신 연도로 답했다: 둘 다 있는 해로 맞춘다
            # (최신 달 값으로 답한 것도 연간 값과 섞지 않는다)
            year = min(y for y in years if y is not None)
            if any(p.get("as_of") for p in parts):
                year = min(year, indicators._data_years()[1])
            parts = both({"year": year})
    for name, p in zip(names, parts, strict=True):
        if p.get("status") != "ok":
            # 재료 하나를 계산하지 못했다 (예: 시군구 자료 없음). 그 안내를 그대로 준다
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
    """이미 계산한 분자(하나 또는 둘) · 분모 결과를 질문 종류(q["type"])대로 나누거나 뺀다.
    값 · 추이 · 비교 · 순위. 사전 지표 결과(compute)와 동적 계산 결과(ask_session._cross_dynamic)가
    같은 길을 쓴다"""
    kind = q.get("type")
    if len(nums) > 1:
        # 분자 두 지표를 더한다 (먼저 둘끼리 지역 · 연도 · 성별이 맞는지 본다)
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
    out = _shell(c, num, den)
    if kind == "trend":
        dv = {s["year"]: s.get("value") for s in den.get("series") or []}
        series = [
            {"year": s["year"], "value": _calc(c, s.get("value"), dv[s["year"]])}
            for s in num.get("series") or []
            if s["year"] in dv
        ]
        series = [s for s in series if s["value"] is not None]
        if len(series) < 2:
            return _fail(
                c,
                f"{josa(c.num_label, '과와')} {josa(c.den, '이가')} "
                "함께 있는 연도가 두 해 이상 없습니다.",
            )
        out.update(region=num.get("region"), sex=num.get("sex"), series=series)
        out["change"] = _change(series, c)
    else:  # compare · rank: 지역마다 나눈다
        key = "values" if kind == "compare" else "ranking"
        dv = {v["region"]: v.get("value") for v in den.get(key) or []}
        vals = [
            {"region": v["region"], "value": _calc(c, v.get("value"), dv.get(v["region"]))}
            for v in num.get(key) or []
        ]
        if any(v["value"] is None for v in vals) or not vals:
            return _fail(c, "어떤 지역은 두 값 중 하나가 없습니다.")
        out.update(year=num.get("year"), sex=num.get("sex"))
        if kind == "compare":
            out.update(_compare_facts(vals, out["unit"]))
        else:
            out.update(_rank_facts(vals, num, q))
    return out


def combine_value(c: Cross, num: dict, den: dict, q: dict) -> dict:
    """값 질문: 분자 · 분모 결과(한 지역 · 한 해)를 나눈 결과. 맞지 않으면 숫자 없이 이유"""
    if why := _mismatch(num, den, {**q, "type": "value"}) or _part_of_whole(c, num, den):
        return _fail(
            c, f"{josa(c.num_label, '과와')} {josa(c.den, '을를')} {c.verb} 수 없습니다: {why}."
        )
    out = _shell(c, num, den)
    out.update(region=num.get("region"), year=num.get("year"), sex=num.get("sex"))
    out["value"] = _calc(c, num.get("value"), den.get("value"))
    out["operands"] = [
        {"label": c.num_label, "value": num.get("value"), "unit": c.unit_of(c.num)},
        {"label": c.den, "value": den.get("value"), "unit": c.unit_of(c.den)},
    ]
    if c.form == "diff":
        out["op"] = "−"
    elif c.scale != 100:
        out["scale"] = c.scale
    return out


def _change(series: list[dict], c: Cross) -> dict:
    """처음 해 대비 마지막 해 변화 (indicators.trend와 같은 칸)"""
    first, last = series[0]["value"], series[-1]["value"]
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
    return out


def _part_of_whole(c: Cross, num: dict, den: dict) -> str | None:
    """'A 중 B 비율'은 B가 A의 일부일 때만: 단위가 같고 B ≤ A (모든 지역 · 연도).
    다른 통계를 따로 찾아 나눌 때 (통근 인구 ÷ 1인 가구 = 322%) 막는다"""
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


def _mismatch(num: dict, den: dict, q: dict) -> str | None:
    """두 결과를 나눠도 되는가. 안 되면 이유"""
    for p in (num, den):
        if p.get("scenario") or "추계" in str(p.get("basis") or ""):
            return "장래 추계 값은 나누지 않습니다"
    sex = q.get("sex") or "계"
    if any((p.get("sex") or "계") != sex for p in (num, den)):
        return f"{josa(_name(num, den, sex), '은는')} 성별로 나뉘어 있지 않습니다"
    if bool(num.get("as_of")) != bool(den.get("as_of")):
        # 주기가 다르다: 연간(12월 말) 값과 KOSIS 월별 자료의 최신 달 값은 섞지 않는다
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
    """결과의 공통 칸: 이름 · 단위 · 계산식 · 출처(분자 표, 분모 표는 extra_sources)"""
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
        # 분모가 분자와 같은 표면 출처를 한 번만 적는다 (출생아 수 · 사망자 수 모두 DT_1B8000H)
        "extra_sources": [
            e for e in extra if e.get("source") and e.get("tbl_id") != num.get("tbl_id")
        ],
        "notes": notes,
        "method": "cross",
        "cross": c.as_dict(),
    }


# 한 시점의 값 ("각 연도 12월 31일 기준", "각 연도 11월 1일"). "1월 1일 ~ 12월 31일"은 기간 값
POINT_DATE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일(?!\s*~)(?:\s*기준)?\s*(?:\(|$|,)")


def _point_date(basis) -> str | None:
    text = str(basis or "")
    if "~" in text:
        return None
    m = POINT_DATE.search(text)
    return f"{int(m[1])}월 {int(m[2])}일" if m else None


def _reference_note(c: Cross, num: dict, den: dict) -> list[str]:
    """두 시점 값의 기준일이 다르면 알린다 (막지는 않는다: 공식 외국인주민 비율도 11월 1일 ÷
    12월 31일이다). 기간 값(1년 동안의 출생아 수)을 시점 값(연말 인구)으로 나누는 것은 보통이다"""
    a, b = _point_date(num.get("basis")), _point_date(den.get("basis"))
    if a and b and a != b:
        return [
            f"두 값의 기준 시점이 다릅니다 ({c.num_label} {a}, {c.den} {b}). "
            "같은 날의 값이 아니라 비율은 근사값입니다"
        ]
    return []


def _sum_parts(parts: list[dict], kind: str) -> dict | None:
    """분자 지표 둘을 더한 결과 (나누기 전). 더할 값이 없으면 None"""
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


def _compare_facts(vals: list[dict], unit: str) -> dict:
    vals = sorted(vals, key=lambda v: v["value"], reverse=True)  # 답 문장은 높은 순으로 읽는다
    hi, lo = vals[0], vals[-1]
    out = {
        "values": vals,
        "highest": hi["region"],
        "lowest": lo["region"],
        "gap": _round(hi["value"] - lo["value"]),
        "gap_unit": "%p" if unit == "%" else unit,
    }
    if len(vals) == 2 and vals[1]["value"]:
        a, b = vals
        out["ratio"] = {
            "a": a["region"],
            "b": b["region"],
            "value": round(a["value"] / b["value"], 2),
        }
    return out


def _rank_facts(vals: list[dict], num: dict, q: dict) -> dict:
    low_first = "낮은" in str(num.get("order") or "")
    ranked = sorted(vals, key=lambda v: v["value"], reverse=not low_first)
    top = q.get("top") or len(ranked)
    return {
        "level": num.get("level"),
        "order": num.get("order"),
        "total_regions": len(ranked),
        "ranking": [{**v, "rank": i} for i, v in enumerate(ranked[:top], start=1)],
    }
