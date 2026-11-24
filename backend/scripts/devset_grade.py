import re
from dataclasses import dataclass, field
from typing import Any

ANSWER, CLARIFY, REFUSE = "답변", "되묻기", "답변 불가"

CLARIFY_STATUSES = {"unclear", "ambiguous_region", "missing_region", "missing_year"}

REFUSAL_GROUPS = {
    "out_of_range": "없는 연도",
    "region_not_in_year": "그해에 없던 지역",
    "no_region_level": "없는 지역 단위",
    "band_mismatch": "맞는 구간 없음",
    "missing_data": "값이 비어 있음",
    "no_joint_table": "함께 집계한 통계 없음",
    "partial_match": "조건을 모두 만족하는 표 없음",
    "out_of_scope": "범위 밖 주제",
    "causal_question": "원인·예측·평가 질문",
    "prediction_question": "원인·예측·평가 질문",
    "not_statistics": "통계 질문이 아님",
    "chat": "통계 질문이 아님",
    "not_collected_item": "집계되지 않는 값",  # 파생값(평균의 평균 · 코호트 출산율 등). 11/24 추가
}
REFUSAL_TYPES = REFUSAL_GROUPS | {
    "unknown_indicator": "범위 밖 주제",
    "not_supported_question": "범위 밖 주제",
}
OLD_REFUSAL_NAMES = {"원인·예측 질문": "원인·예측·평가 질문"}


def golden_refusal_type(label: str) -> str:
    m = re.search(r"\(([a-z_]+)\)\s*$", label or "")
    if m and m[1] in REFUSAL_GROUPS:
        return REFUSAL_GROUPS[m[1]]
    name = (label or "").strip()
    return OLD_REFUSAL_NAMES.get(name, name)


SEARCH_REFUSALS = ("out_of_scope", "not_statistics")


def action_of(res: dict) -> str:
    status = res.get("status")
    if res.get("clarification") or status in CLARIFY_STATUSES:
        return CLARIFY
    tables = res.get("tables") or {}
    if status == "tables" and tables.get("clarify"):
        return CLARIFY
    if status == "ok":
        return ANSWER
    result = res.get("result") or {}
    suggestion = result.get("suggestion") or {}
    if (
        status == "out_of_range"
        and suggestion.get("status") == "ok"
        and not result.get("sub_future")
    ):
        return ANSWER
    return REFUSE


def action_error(expected: str, actual: str) -> str:
    if expected == actual:
        return ""
    if expected == CLARIFY:
        return "되묻기 누락"
    if actual == CLARIFY:
        return "불필요한 되묻기"
    return "그 외"


NUM = r"[+-]?\d[\d,]*(?:\.\d+)?"
NUMBER = re.compile(rf"({NUM})\s*(만|천)?")
URL = re.compile(r"https?://\S+")
TABLE_ID = re.compile(r"\b[A-Z]{2,3}_[0-9A-Z_]+\b")
UNIT_SCALE = {"만": 10_000, "천": 1_000}


@dataclass
class Num:
    value: float
    tol: float
    end: int
    raw: str


def _decimals(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def numbers(text: str) -> list[Num]:
    blank = lambda m: " " * len(m.group(0))  # noqa: E731  (위치를 원문과 맞춘다)
    clean = TABLE_ID.sub(blank, URL.sub(blank, text))
    out = []
    for m in NUMBER.finditer(clean):
        raw, unit = m.group(1), m.group(2)
        after = clean[m.end() : m.end() + 3]
        if unit and re.match(r"\s*명당", after):
            unit = None
        scale = UNIT_SCALE.get(unit, 1)
        value = float(raw.replace(",", "")) * scale
        tol = 0.5 * 10 ** (-_decimals(raw)) * scale
        out.append(Num(value, tol, m.end(), raw))
    return out


def same_number(expected: Num, got: Num) -> bool:
    a, b = abs(expected.value), abs(got.value)
    tol = expected.tol
    if "." in got.raw or got.tol > 0.5:
        tol = max(tol, got.tol)
    return abs(a - b) <= tol + 1e-9


@dataclass
class Expect:
    label: str
    num: Num
    percent_point: bool = False


def _first(pattern: str, text: str) -> Num | None:
    m = re.search(pattern, text)
    if not m:
        return None
    found = numbers(m.group(1))
    return found[0] if found else None


def expected_numbers(kind: str, answer: str) -> list[Expect]:
    text = answer.split("\n")[0]
    out: list[Expect] = []

    def take(label: str, pattern: str, pp: bool = False) -> None:
        if (n := _first(pattern, text)) is not None:
            out.append(Expect(label, n, pp))

    if kind == "집계":
        take("합계", rf"합계\s*({NUM})")
    elif kind == "추이":
        take("증감량", rf"증감량\s*({NUM})")
        take("증감률", rf"증감률\s*({NUM})%")
        take("연평균", rf"연평균\s*({NUM})%")
        take("증감(%p)", rf"증감\s*({NUM})%p", pp=True)
    elif kind == "비교":
        m = re.search(rf"차이\s*({NUM})\s*(%p)?", text)
        if m:
            out.append(Expect("차이", numbers(m.group(1))[0], bool(m.group(2))))
        take("배수", rf"({NUM})배")
    elif kind == "순위":
        for m in re.finditer(rf"(\d+)위\s+(\S+)\s+({NUM})", text):
            out.append(Expect(f"{m.group(1)}위 {m.group(2)}", numbers(m.group(3))[0]))
    elif kind == "구성비":
        take("비율", rf"({NUM})%")
        take("분자", rf"\(\S[^/]*?\s({NUM})\S*\s*/")
        take("분모", rf"/\s*\S[^)]*?\s({NUM})")
    else:
        found = numbers(text)
        if found:
            out.append(Expect("값", found[0]))
    return out


def allowed_answers(answer: str) -> list[str]:
    return [
        re.sub(r"^\s*허용\s*:\s*", "", line)
        for line in answer.split("\n")[1:]
        if line.strip().startswith("허용")
    ]


def rank_names(answer: str) -> list[str]:
    return [m.group(2) for m in re.finditer(rf"(\d+)위\s+(\S+)\s+{NUM}", answer.split("\n")[0])]


def find_number(exp: Expect, nums: list[Num], text: str) -> bool:
    for n in nums:
        if not same_number(exp.num, n):
            continue
        if exp.percent_point and not re.match(r"\s*(%p|%포인트|퍼센트포인트)", text[n.end :]):
            continue
        return True
    return False


def short_name(name: str) -> str:
    name = re.sub(r"(특별자치시|특별자치도|특별시|광역시)$", "", name)
    pairs = {
        "전라남도": "전남",
        "전라북도": "전북",
        "전북특별자치도": "전북",
        "경상남도": "경남",
        "경상북도": "경북",
        "충청남도": "충남",
        "충청북도": "충북",
        "강원특별자치도": "강원",
        "제주특별자치도": "제주",
        "경기도": "경기",
    }
    return pairs.get(name, name)


def ranks_in_order(names: list[str], text: str) -> bool:
    pos = -1
    for name in names:
        pattern = "|".join(re.escape(n) for n in {name, short_name(name)})
        hits = [m.start() for m in re.finditer(pattern, text)]
        nxt = [h for h in hits if h > pos]
        if not nxt:
            return False
        pos = nxt[0]
    return True


YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-9]\d)(?!\d)")
WIDE_SCOPE = re.compile(r"\d+개\s*(시도|구)|시군구|시군|전국 시군구")


def _norm_region(text: str) -> str:
    text = re.sub(r"(특별자치시|특별자치도|특별시|광역시)", "", text)
    return re.sub(r"[\s>]", "", text)


def golden_regions(cond: str) -> list[str] | None:
    first = cond.split(",")[0].strip()
    if WIDE_SCOPE.search(first):
        return None
    parts = re.split(r"\s*(?:·|vs|\+)\s*", first)
    names = []
    for p in parts:
        tokens = p.split()
        if tokens:
            names.append(short_name(tokens[-1]))
    return names


def golden_years(cond: str) -> tuple[int, int] | None:
    years = [int(y) for y in YEAR.findall(cond)]
    return (min(years), max(years)) if years else None


LEAD_YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-9]\d)(?=\s*년)")
LEAD_SPAN = re.compile(r"(?<!\d)(19[5-9]\d|20[0-9]\d)\s*[~∼-]\s*(19[5-9]\d|20[0-9]\d)\s*년")


def lead(res: dict) -> str:
    return (res.get("answer") or "").split("\n\n")[0]


def response_regions(res: dict) -> str:
    parsed = res.get("parsed") or {}
    regions = parsed.get("regions") or []
    if isinstance(regions, str):
        regions = [regions]
    applied = [a.get("value", "") for a in res.get("applied") or [] if a.get("name") == "지역"]
    names = [*regions, *applied, lead(res)]
    if not regions and parsed.get("level") in ("전국", "", None):
        names.append("전국")
    return " ".join(_norm_region(str(r)) + _norm_region(short_name(str(r))) for r in names)


def response_year_set(res: dict) -> set[int]:
    text = lead(res)
    years = {int(y) for pair in LEAD_SPAN.findall(text) for y in pair}
    years |= {int(y) for y in LEAD_YEAR.findall(text)}
    if years:
        return years
    span = response_years(res)
    return set(span) if span else set()


def response_years(res: dict) -> tuple[int, int] | None:
    text = lead(res)
    years = [int(y) for pair in LEAD_SPAN.findall(text) for y in pair]
    years += [int(y) for y in LEAD_YEAR.findall(text)]
    if not years:
        for a in res.get("applied") or []:
            if a.get("name") == "기간":
                years += [int(y) for y in YEAR.findall(str(a.get("value", "")))]
    if not years:
        parsed = res.get("parsed") or {}
        found = [parsed.get(k) for k in ("year", "start_year", "end_year")]
        years = [int(y) for y in found if isinstance(y, int | str) and str(y).isdigit()]
    return (min(years), max(years)) if years else None


def check_conditions(cond: str, res: dict) -> list[str]:
    wrong = []
    regions = golden_regions(cond)
    if regions:
        got = response_regions(res)
        missed = [r for r in regions if _norm_region(r) not in got]
        if missed:
            wrong.append(f"지역 {'·'.join(missed)} 없음")
    want, got_years = golden_years(cond), response_years(res)
    said = response_year_set(res)
    if want and said and not (want[0] in said and want[1] in said):
        span = lambda p: str(p[0]) if p[0] == p[1] else f"{p[0]}~{p[1]}"  # noqa: E731
        wrong.append(f"기간 {span(got_years)} (정답 {span(want)})")
    elif want and not said:
        wrong.append("기간 기록 없음")
    sex = (res.get("parsed") or {}).get("sex")
    if re.search(r",\s*계\s*$", cond) and sex not in (None, "", "계", "전체"):
        wrong.append(f"성별 {sex} (정답 계)")
    elif (want_sex := golden_sex(cond)) and sex != want_sex:
        wrong.append(f"성별 {sex or '없음'} (정답 {want_sex})")
    return wrong


SEX_WORDS = {"남": re.compile(r"남자|남성|남편"), "여": re.compile(r"여자|여성|아내")}


def golden_sex(cond: str) -> str | None:
    found = [s for s, pattern in SEX_WORDS.items() if pattern.search(cond)]
    return found[0] if len(found) == 1 else None


def golden_tables(cell: str) -> tuple[list[str], list[str]]:
    main, allow = [], []
    for line in cell.splitlines():
        ids = TABLE_ID.findall(line)
        (allow if line.strip().startswith("허용") else main).extend(ids)
    return main, allow


def check_sources(cell: str, res: dict) -> tuple[list[str], list[str]]:
    sources = res.get("sources") or []
    missing = []
    if not sources:
        return ["출처 없음"], ["표 없음"]
    for s in sources:
        name = s.get("name") or ""
        if not re.search(r"[가-힣]{2,}", name):
            missing.append("통계표명")
        if not re.search(r"국가데이터처|통계청|행정안전부|법무부|[가-힣]+(부|처|청|원)\b", name):
            missing.append("작성기관")
        if not (s.get("basis") or "").strip():
            missing.append("기준 시점")
        if "kosis.kr" not in (s.get("url") or ""):
            missing.append("링크")
    main, allow = golden_tables(cell)
    got = {s.get("tbl_id") or "" for s in sources}
    got |= {m.group(0) for s in sources for m in TABLE_ID.finditer(s.get("url") or "")}
    matched = got & set(main + allow)
    table_issue = []
    if len(matched) < max(1, len(main)):
        table_issue.append(f"쓴 표 {', '.join(sorted(got - {''})) or '없음'}")
    return sorted(set(missing)), table_issue


CLAIM = re.compile(
    r"때문|원인|탓|덕분|영향으로|영향을\s*받|전망|예상된다|예상됩니다|될\s*것|할\s*것으로"
    r"|가능성이\s*(높|크)|정책|효과가|효과를|바람직|우려|해석된다|보인다|보입니다"
)


def _leaf_numbers(obj: Any, out: list[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, int | float):
        out.append(float(obj))
    elif isinstance(obj, str):
        out.extend(n.value for n in numbers(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _leaf_numbers(v, out)
    elif isinstance(obj, list | tuple):
        for v in obj:
            _leaf_numbers(v, out)


def hallucinated_numbers(res: dict, question: str = "") -> list[str]:
    allowed: list[float] = []
    for key in ("result", "parsed", "applied", "sources", "chart", "tables"):
        _leaf_numbers(res.get(key), allowed)
    _leaf_numbers(question, allowed)
    base = sorted({abs(a) for a in allowed})[:300]
    allowed += [a - b for i, a in enumerate(base) for b in base[:i]]
    text = res.get("answer") or ""
    out = []
    for n in numbers(text):
        after = text[n.end : n.end + 2] if n.end <= len(text) else ""
        if n.value.is_integer() and 1900 <= abs(n.value) <= 2100:
            continue
        if re.match(r"\s*(위|등|번째)", after):
            continue
        if any(abs(abs(n.value) - abs(a)) <= n.tol + 1e-9 for a in allowed):
            continue
        if any(abs(abs(n.value) - abs(a) * 100) <= n.tol + 1e-9 for a in allowed):
            continue
        out.append(n.raw)
    return out


def claim_sentences(answer: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?。])\s+|\n", answer) if CLAIM.search(s)]


@dataclass
class Grade:
    id: str
    set_name: str
    expected: str
    actual: str
    action_error: str = ""
    number: str = ""
    condition: str = ""
    source: str = ""
    refusal: str = ""
    hallucination: int = 0
    correct: str = "X"
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


SETS = {"D": "개발셋", "T": "테스트셋", "N": "신규 테스트셋", "M": "멀티턴 골든셋"}


def grade(row: dict[str, str], first: dict, final: dict | None = None) -> Grade:
    final = final or first
    expected = row["기대 행동"]
    actual = action_of(first)
    g = Grade(
        id=row["문항 ID"],
        set_name=SETS.get(row["문항 ID"][:1], ""),
        expected=expected,
        actual=actual,
        action_error=action_error(expected, actual),
    )
    g.details["status"] = final.get("status")
    g.hallucination = len(halluc := hallucinated_numbers(final, row.get("질문", "")))
    g.details["환각 숫자"] = halluc
    g.details["환각 문장 후보"] = claims = claim_sentences(final.get("answer") or "")

    if expected == REFUSE:
        return _grade_refusal(g, row, final)

    answered = action_of(final) == ANSWER
    text = final.get("answer") or ""
    exp = expected_numbers(row["질문 종류"], row["정답 수치"])
    nums = numbers(text)
    found = {e.label: find_number(e, nums, text) for e in exp}
    for alt in allowed_answers(row["정답 수치"]):
        alt_exp = expected_numbers(row["질문 종류"], alt)
        alt_found = {e.label: find_number(e, nums, text) for e in alt_exp}
        if alt_exp and all(alt_found.values()) and not all(found.values()):
            exp, found = alt_exp, alt_found
            g.details["허용 정답"] = alt
    if row["질문 종류"] == "순위":
        found["순서"] = ranks_in_order(rank_names(row["정답 수치"]), text)
    computed: list[float] = []
    _leaf_numbers(final.get("result"), computed)
    in_result = all(
        any(abs(abs(e.num.value) - abs(c)) <= max(e.num.tol, 1e-9) + 1e-9 for c in computed)
        or any(abs(abs(e.num.value) - abs(c) * 100) <= e.num.tol for c in computed)
        for e in exp
    )
    g.details["맞아야 하는 숫자"] = {e.label: e.num.raw for e in exp}
    g.details["찾은 숫자"] = found
    g.number = "O" if answered and exp and all(found.values()) else "X"

    cond = row["정답 조건"]
    if (alt := g.details.get("허용 정답")) and golden_years(alt):
        lo, hi = golden_years(alt)
        cond = YEAR.sub("", cond) + (f", {lo}" if lo == hi else f", {lo}~{hi}")
    cond_wrong = check_conditions(cond, final) if answered else ["답하지 않음"]
    g.details["조건 차이"] = cond_wrong
    g.condition = "X" if cond_wrong else "O"

    missing, table_issue = check_sources(row["정답 표"], final) if answered else (["출처 없음"], [])
    g.details["빠진 출처"] = missing
    g.details["표 차이"] = table_issue
    if table_issue and g.number == "O" and g.condition == "O":
        g.details["표 차이"] = [f"{t} (숫자가 맞아 인정)" for t in table_issue]
        table_issue = []
    g.source = "X" if missing or table_issue else "O"

    ok = not g.action_error and g.number == "O" and g.condition == "O" and g.source == "O"
    g.correct = "O" if ok else "X"
    if not ok:
        g.reason = _first_wrong(g, answered, table_issue, cond_wrong, in_result, claims)
    return g


def _first_wrong(g, answered, table_issue, cond_wrong, in_result, claims) -> str:
    if g.action_error:
        return f"행동 ({g.action_error})"
    if not answered:
        return "행동 (최종 답 없음)"
    if table_issue:
        return "표"
    if cond_wrong:
        return "조건"
    if g.number == "X" and not in_result:
        return "계산"
    return "요약"


def _grade_refusal(g: Grade, row: dict[str, str], res: dict) -> Grade:
    status = res.get("status")
    tables = res.get("tables") or {}
    if status == "tables" and tables.get("status") in SEARCH_REFUSALS:
        status = tables["status"]
        g.details["거절 근거"] = tables.get("reason") or "(기록 없음)"
    g.details["안내 틀 ID"] = status
    want = golden_refusal_type(row.get("답변 불가 유형", ""))
    got = REFUSAL_TYPES.get(status or "", "")
    g.details["불가 유형"] = got or f"(정하지 못함: {status})"
    result = res.get("result") or {}
    alternative = bool(
        result.get("available_years")
        or result.get("suggestion")
        or result.get("alternative")
        or tables.get("recommendations")
        or re.search(r"대신|볼 수 있|확인할 수 있|해 주세요", res.get("answer") or "")
    )
    if g.action_error:
        g.refusal, g.reason = "", f"행동 ({g.action_error})"
    elif got != want:
        g.refusal, g.reason = "유형 오류", "행동 (불가 유형 오류)"
    elif not alternative:
        g.refusal, g.reason = "대안 없음", "요약 (대안 없음)"
    else:
        g.refusal = "O"
    g.correct = "O" if g.refusal == "O" else "X"
    return g
