"""질문의 조건 → 표의 칸 잇기 (docs/cell_binding_design_2026-11-23.md)

1단계: 바로 계산(dynamic_indicator)이 표 하나를 계산할 때 같은 표로 잇기를 돌려, 결과와
지금 계산이 고른 칸을 처리 기록(trace["dynamic"]의 그 표 칸 "binding")에 나란히 남긴다.
답 · 계산 · 표 순위는 바꾸지 않는다. BINDING_SHADOW=true 일 때만 돈다.

잇기는 조건마다 "어느 칸(또는 표 전체)에 이어졌나 · 어떻게(by)"를 남긴다:
  value     칸 이름이 질문 낱말과 맞음 ('3자녀', '출생자녀 3명 ~ 10명이상')
  table     표 전체가 이미 그 조건
            (기혼여성 표의 성별 '여', '고령자(65세이상) 가구'의 65세 이상)
  dim       나눠 보기('~별')의 분류 전체
  age_range 연령 분류 (칸은 계산이 연령 구간으로 고른다)
  region    지역 분류 (칸은 계산이 지역 이름으로 고른다)
  period    수록 기간 안
  total · default · couple · split
            말하지 않은 분류를 합계 · 기본값 · 부부 단위 남편 칸 · 남녀 따로로
지금 통계표 찾기(search.check)와 계산(dynamic_indicator.choose · uncovered)에 흩어진 규칙을
모은다. LLM은 쓰지 않는다 (2단계)
"""

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.services import agegroup, lookup, search
from app.services import dynamic_indicator as di

logger = logging.getLogger(__name__)

BOUND, PARTIAL = "bound", "partial"
MISSED_AGE = "질문 이해에 없는 연령"

# 수 조건 ('딱 셋', '셋 이상', '3명 이상'). 나이(살 · 세 · 대)는 연령 조건이라 뺀다
NATIVE_COUNT = {
    "하나": 1, "둘": 2, "셋": 3, "넷": 4, "다섯": 5,
    "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}  # fmt: skip
SHORT_COUNT = {"한": 1, "두": 2, "세": 3, "네": 4}  # 뒤에 세는 말이 올 때만 ('세 명', '두 자녀')
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
AGE_CELL = re.compile(r"\d+\s*(?:세|살|대)")  # '3세' · '20대'는 수가 아니라 나이 칸 (DT_1YC1601)
MIN_COUNT_CELLS = 3  # 수로 된 칸이 이만큼 있는 분류만 수 조건으로 잇는다


@dataclass
class Bound:
    cond: str  # sex · age · item · class · region · year · word(질문에 나온 칸) · 분류 이름(기본값)
    value: str  # 질문의 말 또는 조건 값
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
    skipped: list[str] = field(default_factory=list)  # 1단계에서 보지 않는 조건

    @property
    def status(self) -> str:
        return PARTIAL if self.unbound else BOUND

    def picks(self) -> dict[int, list[str]]:
        """분류 번호 → 이은 칸 코드 (칸을 정한 것만. 연령 · 지역은 계산이 고른다)"""
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
    """'65살 이상' → '65세 이상' (agegroup은 '세'만 읽는다)"""
    return agegroup.native_to_digits(re.sub(r"(\d+)\s*살", r"\1세", text or ""))


def _age_span(text: str) -> tuple[int, int | None] | None:
    """연령 표현 → (시작, 끝). 끝이 None이면 이상. 나이 하나('65세')는 (65, 65)"""
    t = _age_text(text)
    if a := agegroup.span(t):
        return a.lo, a.hi
    if one := agegroup.single_age(t):
        n = int(one.removesuffix("세"))
        return n, n
    return None


def _count_ask(text: str) -> tuple[int, str] | None:
    """'셋 이상' → (3, '>='), '딱 셋' → (3, '=='), '2명 이하' → (2, '<=')"""
    m = COUNT_ASK.search(text or "")
    if not m:
        return None
    raw = m["n"]
    n = int(raw) if raw.isdigit() else NATIVE_COUNT.get(raw) or SHORT_COUNT[raw]
    op = {"이상": ">=", "넘": ">", "초과": ">", "이하": "<=", "미만": "<"}.get(m["op"] or "", "==")
    return n, op


def _count_cells(ask: tuple[int, str], d: di.Dim) -> list[str]:
    """수 조건에 맞는 칸 ('출생자녀 3명' ~ '출생자녀 10명이상').
    수로 된 칸이 적은 분류는 보지 않는다"""
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


# 조건 낱말과 칸 이름이 서로를 담을 때, 짧은 쪽이 긴 쪽의 이만큼은 돼야 같은 것으로 본다.
# '부부 및 미혼 형제자매'(9자) 안의 '미혼'(2자) 칸에 잇지 않는다 (D-144).
# '지하철' ↔ '전철 지하철'(3/5)은 잇는다
MIN_OVERLAP = 0.5


def _related(word: str, name: str) -> bool:
    """조건 낱말과 칸 이름이 같은 것을 가리키나 (한쪽이 다른 쪽을 담고, 길이가 크게 다르지 않음)"""
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
    """표 하나에서 질문의 조건을 칸에 잇는다. 적재 안 된 표면 StatsTableMissing
    forced: 되묻기에서 사용자가 고른 칸 · 칸 고르기 LLM이 고른 칸 (그대로 쓴다)"""
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
    # 성별은 질문 이해(parsed)의 것만 본다 (지금 계산과 같다). 통계표 찾기의 성별은
    # '외국인 아내'처럼 배우자를 가리키기도 한다 (V2-001 '베트남 여자랑 결혼' → 외국인 아내 국적별)
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
            # 기본(중위) 추계가 없는 특별 추계 표(DT_1BPA401: 높은 출산율 · 현수준 ·
            # 빠른 고령화 …)는 시나리오를 말하지 않은 질문에 맞는 칸이 없다
            # → 다음 후보 표(중위 추계)로 (V2-050)
            why = "시나리오를 말하지 않았는데 기본(중위) 추계가 없는 표"
            out.unbound.append(Unbound(d.name, d.codes[code], why))
            continue
        if default is not None and code != default and not said:
            # 기본값이 정해진 분류(장래추계 시나리오 → 중위)를 질문이 말하지 않았는데 칸 고르기
            # LLM이 다른 칸을 골랐다: 기본값으로 (V2-050 '출산율 현수준 추계'로 답했다)
            forced[no] = code = default
            name = d.codes[code]
            out.bound.append(Bound(d.name, name, "default", no, [code], [name], note="LLM 칸 대신"))
            continue
        out.bound.append(Bound("forced", d.codes[code], "forced", no, [code], [d.codes[code]]))
    ages = parsed.get("ages") or []
    age = tuple(ages[0]) if ages else None

    # 성별을 말함
    if sex in ("남", "여") and not (sex_dim is not None and sex_dim.no in forced):
        if sex_dim is not None:
            code = sex_dim.sex_codes()[sex]
            out.bound.append(Bound("sex", sex, "value", sex_dim.no, [code], [sex_dim.codes[code]]))
        elif sex == "여" and search.FEMALE_ONLY_TABLE.search(name_n):
            out.bound.append(Bound("sex", sex, "table", note="표 전체가 여성"))
        else:
            out.unbound.append(Unbound("sex", sex, "표에 성별 구분이 없음"))

    # 연령
    if age:
        label = _age_label(age)
        if age_dim is not None:
            out.bound.append(Bound("age", label, "age_range", age_dim.no, note=age_dim.name))
        elif parsed.get("age_group"):
            # 지금 계산과 같다: 말에서 짐작한 연령(노년부양비의 노년)은 지표 이름의 일부일 수 있다
            out.skipped.append(f"age {label} (지표 이름의 일부로 봄)")
        elif cells := _age_cells(age, dims, region_dim):
            # 연령 분류는 없지만 그 연령이 칸 이름에 든 표 ('가구원 모두 65세이상'). 표 이름의 연령
            # 범위보다 먼저 본다: '고령자(65세이상) 가구' 표도 65세 이상 없는 가구까지 담는다
            no, codes = cells
            d = by_no[no]
            if forced.get(no) in codes:
                # 그 연령이 든 칸이 여럿인데 칸 고르기 LLM이 그중 하나를 골랐다 ('식구가 전부 65살
                # 이상' → 가구원 모두 65세이상, V2-013)
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
    used: set[tuple[int, str]] = set()  # 조건으로 이미 쓴 (분류, 칸)

    # 찾는 값 (통계표 찾기의 items)
    for item in cond.get("items") or []:
        if b := _bind_item(item, dims, skip, hits, titles, age, age_dim, out):
            out.bound.append(b)
            used |= {(b.dim, c) for c in b.codes if b.dim is not None}
        elif b is None:
            out.unbound.append(Unbound("item", item, "맞는 칸 없음"))

    # 나눠 보기 (~별)
    for cls in cond.get("classes") or []:
        d = _class_dim(cls, dims, region_dim, sex_dim, age_dim)
        aged = any(x.cond == "age" or x.by == "age_range" for x in out.bound)
        if d is not None and d is age_dim and aged:
            # 연령 조건이 이미 있으면 '연령별'은 그 연령이다 ('스무 살 안 됐는데' + 연령별
            # → 20세 미만 하나, 연령 칸 모두의 비교가 아니다, V2-144)
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

    # 질문에 이름이 나온 칸 (조건으로 쓰지 않은 것) · 말하지 않은 분류
    taken = {b.dim for b in out.bound if b.dim is not None}
    multi = di._multi_op(question)
    for d in dims:
        if d.no in skip or d.no in taken:
            continue
        codes = [c for c in hits.get(d.no, []) if (d.no, c) not in used]
        if d is sex_dim:
            _bind_sex_default(d, codes, couple, age, out)
        elif len(codes) > 1 and d.total() and set(codes) == set(di._leaf_codes(d)):
            # 세부 칸을 모두 말함 ('출퇴근하거나 학교 다닌' = 통근 + 통학) → 그 분류의 합계
            # (지금 계산의 choose와 같다)
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


# 질문의 말로 이은 것 (지금 계산의 plan.matched에 해당)
MATCHED_BY = {"value", "forced", "dim"}


def _bind_topic(slots, parsed, sex, sex_dim, out: Binding) -> None:
    """질문과 이 표를 잇는 말이 하나도 없으면 잇지 못한 것으로 둔다. 말하지 않은 분류를 모두 합계로
    채우면 묻지 않은 표의 합계를 답하게 된다 ('아이 키우기 좋은 동네' → 출생아 수 합계).
    지금 계산의 "맞는 말이 없는 표는 쓰지 않는다"와 같은 기준이다. 주제 낱말이 표 이름에 있다는
    것만으로는 잇지 않는다 ('외국인 아내 · 남편 비교'가 외국인 남편 표 하나로 이어졌다, D-063)"""
    # 통계표 찾기의 조건(찾는 값 · 나눠 보기)을 표의 연령 분류에 이은 것도 질문과 표를 잇는 말이다
    # ('스무 살 안 됐는데' → 20세미만, V2-144)
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
    """장래추계 시나리오 분류인가 (칸 이름 대부분이 '…추계')"""
    names = [search.norm(n) for n in d.codes.values()]
    return len(names) >= 2 and sum("추계" in n for n in names) * 2 > len(names)


def _age_cells(age: tuple, dims: list, region_dim: int | None) -> tuple[int, list[str]] | None:
    """연령 분류(합계 칸이 있는 연령별)가 없는 표에서 이름에 그 연령 범위가 든 칸 → (분류, 코드들).
    칸 이름 대부분에 나이가 든 항목('가구원 모두 65세이상' …)도 본다"""
    for d in dims:
        if d.no == region_dim:
            continue
        codes = [c for c, n in d.codes.items() if _age_span(n) == age]
        if codes:
            return d.no, codes
    return None


def _within(span: tuple, age: tuple) -> bool:
    """연령 구간 span이 age 안에 드나 (끝이 None이면 이상)"""
    lo, hi = age
    s_lo, s_hi = span
    return lo <= s_lo and (hi is None or (s_hi is not None and s_hi <= hi))


def _age_label(age: tuple) -> str:
    lo, hi = age
    return f"{lo}세 이상" if hi is None else f"{lo}~{hi}세"


def _bind_item(item, dims, skip, hits, titles, age, age_dim, out) -> "Bound | None | bool":
    """찾는 값 하나 → Bound. 연령 조건과 같은 말이면 False (연령에서 이미 봄). 못 이으면 None"""
    key = search.norm(item)
    # 0. 되묻기 · 칸 고르기 LLM이 고른 칸이 이미 이 조건을 담음 ('이혼' → 내국인-이혼, V2-016).
    # 지금 계산의 조건 반영 검사(_item_left: 고른 칸 이름에 낱말이 있으면 반영)와 같다
    words = _words(item) or ([key] if len(key) >= 2 else [])
    for b in out.bound:
        if b.by == "forced" and words and all(w in search.norm(b.names[0]) for w in words):
            return Bound("item", item, "forced", b.dim, list(b.codes), list(b.names))
    # 1. 질문에 이름이 나온 칸 중 이 낱말을 가리키는 칸
    for no, codes in hits.items():
        d = next(x for x in dims if x.no == no)
        near = [c for c in codes if _related(item, d.codes[c])]
        if near:
            return Bound("item", item, "value", no, near, [d.codes[c] for c in near])
    # 2. 수 조건 ('셋 이상' → 출생자녀 3명 ~ 10명이상)
    if ask := _count_ask(item):
        for d in dims:
            if d.no in skip:
                continue
            if found := _count_cells(ask, d):
                names = [d.codes[c] for c in found]
                return Bound("item", item, "value", d.no, found, names, note="수 범위")
    # 2-2. 조건의 낱말이 모두 든 칸이 하나뿐 (수 조건 다음: '자녀가 딱 셋'을 '자녀가 있는 가구_계'에
    # 잇지 않게) ('부부 및 미혼 형제자매' → 1세대가구-부부+미혼형제자매)
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
    # 3. 낱말 줄기 ('가구수' ↔ '가구_계')
    for d in dims:
        if d.no in skip:
            continue
        same = [c for c, n in d.codes.items() if search.stem(n) == search.stem(key)]
        if len(same) == 1:
            return Bound("item", item, "value", d.no, same, [d.codes[same[0]]], note="낱말 줄기")
    # 4. 연령 표현 ('65살 이상'): 연령 조건으로 본다
    if (span := _age_span(item)) is not None:
        if age is not None and _within(span, age):
            # 질문 이해가 넘긴 연령에 들어 있다 (연령에서 봄). 통계표 찾기는 '65살부터 72살까지'의
            # 양 끝을 '65세' · '72세' 조건으로 따로 뽑기도 한다 (V2-137)
            return False
        if age is None and age_dim is not None:
            # 질문 이해가 놓친 연령을 통계표 찾기가 조건으로 뽑았다 ('85세 넘은 분', D-144).
            # 지금 계산은 이 연령 없이 전체 연령으로 답했다 → compare가 '다름'으로 드러낸다
            return Bound("item", item, "age_range", age_dim.no, note=MISSED_AGE)
        if span == _age_span(titles[0]):
            return Bound("item", item, "table", note="표 이름의 연령 범위")
    # 5. 표 자체가 그 조건으로 만든 표 ('재혼' → 평균재혼연령)
    if len(key) >= 2 and any(key in t for t in titles):
        return Bound("item", item, "table", note="표 이름")
    return None


JOINERS = {"및", "와", "과", "랑", "이랑", "하고", "또는", "그리고", "중", "의"}


def _words(text: str) -> list[str]:
    """조건을 낱말로 ('부부 및 미혼 형제자매' → 부부 · 미혼 · 형제자매). 잇는 말은 뺀다"""
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
    """성별을 말하지 않음: 합계 → 대상 전체 칸 → 부부 단위 남편 칸 → 남녀 따로 (#208과 같은 순서)"""
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
    # 남편 칸 하나로 답하는 것은 부부를 한 쪽씩 센 건수 표뿐이다 (값으로 판정, couple_counted).
    # 평균 초혼연령처럼 남편 · 아내 값이 다른 표는 남녀 따로 (지금 계산은 남편 · 아내 분류면 남편
    # 칸을 골랐다: V2-116 '결혼 나이 평균')
    husband = (di._couple_code(d) or d.sex_codes()["남"]) if couple else None
    if husband and not age:
        name = d.codes[husband]
        out.bound.append(Bound(d.name, name, "couple", d.no, [husband], [name]))
        return
    both = [d.sex_codes()["남"], d.sex_codes()["여"]]
    out.bound.append(Bound(d.name, "남 · 여", "split", d.no, both, [d.codes[c] for c in both]))


def _bind_default(d: "di.Dim", couple: bool, age, out: Binding) -> None:
    """말하지 않은 분류: 합계 → 기본값 → 칸이 하나 → 남편 칸 → 못 정함 (되묻기)"""
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


# ---- 지금 계산과 견주기 -------------------------------------------------------------------

SAME, DIFFER = "같음", "다름"
NOT_COMPARED = "견주지 않음 (비율 · 구성비)"  # 분자 · 분모는 칸이 아니라 계산이 정한다
# 지금 계산도 칸은 골랐는데 그 뒤 계산 단계의 검사('쌍' 단위, 부분 ≤ 전체 등)에서 막혔고, 고른 칸이
# 잇기와 같다. 잇기로 바꿔도 같은 검사에서 막힌다 (잇기만 성공으로 세지 않는다)
CHECK_BLOCKED = "칸 같음 · 계산 검사에서 막힘"
ONLY_BINDING, ONLY_CURRENT, BOTH_FAIL = "잇기만 성공", "지금 계산만 성공", "둘 다 실패"


def current_picks(plan: "di.Plan | None") -> dict[int, list[str]]:
    """지금 계산이 고른 칸 (분류 번호 → 코드). 한 분류 안의 연산 · 남녀 따로도 넣는다"""
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
    """잇기 결과 ↔ 지금 계산. ok: 지금 계산이 정의를 만들었나 (build가 spec을 돌려줌)"""
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
            continue  # 한쪽만 정한 분류(연령 · 지역 등 계산이 고르는 분류)는 견주지 않는다
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
    """처리 기록에 남길 잇기 결과 + 지금 계산과의 비교. 잇기가 실패해도 계산에는 영향이 없다"""
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


# ---- 2단계: 잇기 결과로 다시 계산 -----------------------------------------------------------

# 순위에서 빼는 칸 (V2-077 골든: 외국인 남편 국적별 상위 3에서 '기타 및 미상' 제외)
RANK_SKIP = re.compile(r"기타|미상")


@dataclass
class Retry:
    """잇기 결과를 지금 계산(build)에 넘기는 모양"""

    forced: dict[int, str]  # 칸 하나로 정한 분류 (되묻기 · 칸 고르기 LLM과 같은 길)
    multi: (
        tuple[int, list[str], str] | None
    )  # 칸 여럿으로 하는 한 분류 안의 연산 (합 · 비교 · 순위)
    parsed: dict  # 표 전체가 만족한 조건은 뺀 질문 해석 (성별 '여' → 표 전체가 기혼여성)

    def to_dict(self) -> dict[str, Any]:
        return {"forced": self.forced, "multi": list(self.multi) if self.multi else None}


def retry_plan(b: Binding, question: str, parsed: dict) -> Retry | None:
    """모두 이은 잇기 결과 → 지금 계산에 넘길 칸. 한 분류 안의 연산이 둘 이상이면 None
    (parts_answer는 분류 하나만 묶는다)"""
    if b.status != BOUND:
        return None
    if parsed.get("type") == "composition" or di.PCT_ASK.search(question):
        # 비율 · 구성비는 분자 · 분모를 계산이 정한다. 잇기의 칸으로 다시 계산하면 분자와 분모가
        # 같은 칸이 되기도 한다 ('식구가 전부 65살 이상인 집은 몇 퍼센트' → 100%, V2-090)
        return None
    dims = {d.no: d for d in di.schema(b.tbl_id)[0]}
    forced: dict[int, str] = {}
    multi = None
    q = dict(parsed)
    for x in b.bound:
        if x.by == "table" and x.cond == "sex":
            q["sex"] = None  # 표 전체가 그 성별: 성별 분류를 찾지 않는다
        if x.by in ("table", "value", "forced") and x.cond == "age":
            q["ages"] = None  # 표 전체 · 칸 하나가 그 연령 (그 칸은 forced로 넘긴다)
        if x.note == MISSED_AGE and (span := _age_span(x.value)):
            q["ages"] = [list(span)]  # 질문 이해가 놓친 연령: 계산이 연령 칸으로 고른다
        if x.dim is None or x.by in ("age_range", "region", "split"):
            continue  # 연령 · 지역은 계산이 고르고, 남녀 따로는 choose가 같은 규칙으로 한다
        if x.by == "dim":
            d = dims[x.dim]
            rank = parsed.get("type") == "rank" or bool(di.TOP_N.search(question))
            codes = [c for c in di._leaf_codes(d) if not (rank and RANK_SKIP.search(d.codes[c]))]
            group = (x.dim, codes, "rank" if rank else "compare")
        elif len(x.codes) > 1:
            group = (x.dim, list(x.codes), di._multi_op(question) or "sum")
        else:
            if forced.get(x.dim, x.codes[0]) != x.codes[0]:
                return None  # 한 분류에 다른 칸 둘
            forced[x.dim] = x.codes[0]
            continue
        if multi is not None and multi[0] != group[0]:
            return None
        multi = group
    if multi is not None:
        forced.pop(multi[0], None)
    return Retry(forced, multi, q)
