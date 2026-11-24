"""개발셋·테스트셋 자동 채점 규칙 (챗봇 호출 없이 응답 하나를 채점하는 순수 함수)

골든셋 시트 한 줄(문항)과 챗봇 응답(/ask 응답과 같은 dict)을 받아
"채점 기준표"의 칸을 채운다: 실제 행동, 숫자, 조건, 출처, 답변 불가, 환각, 정답, 틀린 이유.
실행은 scripts/eval_devset.py.

골든셋 칸 (구글 시트 "골든셋_개발셋_v1"의 머리글 그대로)
  문항 ID, 질문, 기대 행동(답변 / 되묻기 / 답변 불가), 질문 종류, 정답 표, 정답 조건,
  적용 기본값, 정답 수치, 데이터 경로(적재 / API), 필요 표 수, 사용자 답, 답변 불가 유형,
  정답 확인 근거

정답 수치 칸은 질문 종류별로 이렇게 쓴다 (이 모양을 읽어 맞아야 하는 숫자를 뽑는다)
  단순 수치·인구 통계 지표  첫 숫자              "556,330명", "합계출산율 0.799명"
  집계                      "합계 N"              "합계 42,854명 (20~24세 … + …)"
  추이                      증감량·증감률·연평균  "… 증감량 +14,431명, 증감률 +19.7%, 연평균 +4.6%"
                            (비율 추이는 "증감 +0.62%p")
  비교                      "차이 N", "N배"       "전남 28.5%, 서울 20.4%. 차이 8.0%p, 1.40배"
  순위                      "N위 이름 값"         "1위 전라남도 1.094명; 2위 …"
  구성비                    첫 "N%"와 분자·분모   "21.2% (65세 이상 10,840,822명 / 총인구 …)"
"""

import re
from dataclasses import dataclass, field
from typing import Any

# ---- 행동 ---------------------------------------------------------------------------------

ANSWER, CLARIFY, REFUSE = "답변", "되묻기", "답변 불가"

# 되묻기로 보는 status (지역·연도를 되물음)
CLARIFY_STATUSES = {"unclear", "ambiguous_region", "missing_region", "missing_year"}

# 답변불가 유형정의서: 코드 → 유형 이름. 골든셋은 "유형 이름 (코드)"로 적는다.
# 코드가 둘인 유형(원인·예측·평가, 통계 질문이 아님)은 같은 유형이면 맞음
REFUSAL_GROUPS = {
    "out_of_range": "없는 연도",
    "region_not_in_year": "그해에 없던 지역",
    "no_region_level": "없는 지역 단위",
    "band_mismatch": "맞는 구간 없음",  # 표의 구간과 맞지 않음 (연령 구간, 체중·기간 같은 조건)
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
# 챗봇 status → 유형. 정의서에 없는 status는 가까운 유형으로.
# 여기 없는 status는 유형을 정하지 못한 것(유형 오류)
# 통계표 찾기의 거절(응답의 tables.status)도 같은 코드를 쓴다. 근거는 tables.reason
REFUSAL_TYPES = REFUSAL_GROUPS | {
    "unknown_indicator": "범위 밖 주제",
    "not_supported_question": "범위 밖 주제",
}
# 코드 없이 적은 옛 표기
OLD_REFUSAL_NAMES = {"원인·예측 질문": "원인·예측·평가 질문"}


def golden_refusal_type(label: str) -> str:
    """골든셋 답변 불가 유형 칸 → 유형 이름.
    "그해에 없던 지역 (region_not_in_year)" → 그해에 없던 지역. 코드 없는 옛 표기는 이름 그대로"""
    m = re.search(r"\(([a-z_]+)\)\s*$", label or "")
    if m and m[1] in REFUSAL_GROUPS:
        return REFUSAL_GROUPS[m[1]]
    name = (label or "").strip()
    return OLD_REFUSAL_NAMES.get(name, name)


SEARCH_REFUSALS = ("out_of_scope", "not_statistics")


def action_of(res: dict) -> str:
    """챗봇 응답 → 답변 / 되묻기 / 답변 불가.
    수치 없이 통계표만 안내한 답(status "tables")은 수치로 답하지 못한 것이라 답변 불가로 본다.
    수록 기간 밖이지만 추계값으로 대신 답한 경우(out_of_range + suggestion ok)는 답변이다"""
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
    # sub_future: 시군구의 미래 연도를 물어 속한 시도의 추계값을 참고로 보여 준 것은 답이 아니다
    if (
        status == "out_of_range"
        and suggestion.get("status") == "ok"
        and not result.get("sub_future")
    ):
        return ANSWER
    return REFUSE


def action_error(expected: str, actual: str) -> str:
    """행동이 틀렸을 때 기록할 구분 (채점 기준표: 되묻기 누락 / 불필요한 되묻기 / 그 외)"""
    if expected == actual:
        return ""
    if expected == CLARIFY:
        return "되묻기 누락"
    if actual == CLARIFY:
        return "불필요한 되묻기"
    return "그 외"


# ---- 숫자 ---------------------------------------------------------------------------------

NUM = r"[+-]?\d[\d,]*(?:\.\d+)?"
NUMBER = re.compile(rf"({NUM})\s*(만|천)?")
URL = re.compile(r"https?://\S+")
TABLE_ID = re.compile(r"\b[A-Z]{2,3}_[0-9A-Z_]+\b")
UNIT_SCALE = {"만": 10_000, "천": 1_000}


@dataclass
class Num:
    value: float  # 단위 환산 뒤 값
    tol: float  # 이 숫자가 적힌 자릿수에서 반올림 오차 (환산 뒤)
    end: int  # 문장 속 끝 위치 (뒤에 오는 글자를 볼 때)
    raw: str


def _decimals(text: str) -> int:
    return len(text.split(".")[1]) if "." in text else 0


def numbers(text: str) -> list[Num]:
    """문장 속 숫자. 링크·표 ID 안의 숫자는 뺀다. "5.2만"은 52,000으로 환산한다.
    "1천 명당"처럼 단위 설명의 천은 환산하지 않는다"""
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
    """반올림 차이와 단위 환산을 허용해 같은 수인가 (부호는 보지 않는다: "-975명" = "975명 감소")

    답 쪽 반올림 오차는 답이 자릿수를 줄여 적었거나(0.107 → 0.11)
    만·천 단위로 적은 경우(160만)만 쓴다. 단위 없는 정수까지 허용하면
    "1세별" · "1월 1일" · "가임여성 1명당"의 1이 배수 1.17과,
    "연령(5세)"의 5가 지표 5.2와 같은 수가 된다 (2026-11-20 테스트셋 1차에서 발견)
    """
    a, b = abs(expected.value), abs(got.value)
    tol = expected.tol
    if "." in got.raw or got.tol > 0.5:
        tol = max(tol, got.tol)
    return abs(a - b) <= tol + 1e-9


@dataclass
class Expect:
    """맞아야 하는 숫자 하나"""

    label: str
    num: Num
    percent_point: bool = False  # %p로 표시해야 정답 (비율 차이)


def _first(pattern: str, text: str) -> Num | None:
    m = re.search(pattern, text)
    if not m:
        return None
    found = numbers(m.group(1))
    return found[0] if found else None


def expected_numbers(kind: str, answer: str) -> list[Expect]:
    """골든셋 정답 수치 칸 → 질문 종류별로 맞아야 하는 숫자"""
    text = answer.split("\n")[0]  # 둘째 줄부터는 참고·주의
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
    else:  # 단순 수치, 인구 통계 지표
        found = numbers(text)
        if found:
            out.append(Expect("값", found[0]))
    return out


def allowed_answers(answer: str) -> list[str]:
    """정답 수치 칸의 "허용:" 줄 → 다른 기준으로 맞는 답 (정답 표 칸의 "허용:"과 같은 표기).
    예) 부양비 정답은 인구총조사 공표값, 허용 줄은 주민등록인구로 계산한 값 (11/24)"""
    return [
        re.sub(r"^\s*허용\s*:\s*", "", line)
        for line in answer.split("\n")[1:]
        if line.strip().startswith("허용")
    ]


def rank_names(answer: str) -> list[str]:
    """순위 정답의 이름 순서 ("1위 전라남도 …; 2위 세종특별자치시 …")"""
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
    """ "전라남도" → "전남", "세종특별자치시" → "세종": 답변이 줄임말로 써도 찾는다"""
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
    """이름이 정답 순서대로 나오는가. 정식 명칭("전라남도")과 줄임말("전남") 모두 찾는다"""
    pos = -1
    for name in names:
        pattern = "|".join(re.escape(n) for n in {name, short_name(name)})
        hits = [m.start() for m in re.finditer(pattern, text)]
        nxt = [h for h in hits if h > pos]
        if not nxt:
            return False
        pos = nxt[0]
    return True


# ---- 조건 ---------------------------------------------------------------------------------

YEAR = re.compile(r"(?<!\d)(19[5-9]\d|20[0-9]\d)(?!\d)")
WIDE_SCOPE = re.compile(r"\d+개\s*(시도|구)|시군구|시군|전국 시군구")


def _norm_region(text: str) -> str:
    text = re.sub(r"(특별자치시|특별자치도|특별시|광역시)", "", text)
    return re.sub(r"[\s>]", "", text)


def golden_regions(cond: str) -> list[str] | None:
    """정답 조건 첫 칸의 지역들 (시군구는 마지막 이름). 순위처럼 전체를 보는 범위면 None"""
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
    """답변 첫 문단: 챗봇이 실제로 계산한 지역·기간이 쓰여 있다
    (parsed·applied는 HCX 1차 해석이라 계산 때 바로잡힌 기간이 반영되지 않을 수 있다)"""
    return (res.get("answer") or "").split("\n\n")[0]


def response_regions(res: dict) -> str:
    parsed = res.get("parsed") or {}
    regions = parsed.get("regions") or []
    if isinstance(regions, str):
        regions = [regions]
    applied = [a.get("value", "") for a in res.get("applied") or [] if a.get("name") == "지역"]
    names = [*regions, *applied, lead(res)]
    if not regions and parsed.get("level") in ("전국", "", None):  # 전국은 regions가 비어 있다
        names.append("전국")
    return " ".join(_norm_region(str(r)) + _norm_region(short_name(str(r))) for r in names)


def response_year_set(res: dict) -> set[int]:
    """답에 나온 연도들 (답변 첫 문단 → applied → parsed 순으로 처음 찾은 곳)"""
    text = lead(res)
    years = {int(y) for pair in LEAD_SPAN.findall(text) for y in pair}
    years |= {int(y) for y in LEAD_YEAR.findall(text)}
    if years:
        return years
    span = response_years(res)
    return set(span) if span else set()


def response_years(res: dict) -> tuple[int, int] | None:
    """실제로 답한 기간. 답변 첫 문단의 연도를 먼저 보고, 없으면 applied·parsed"""
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
    """정답 조건과 다른 점 목록 (비면 맞음). 지역·기간·성별만 본다 (대상은 숫자로 확인된다)"""
    wrong = []
    regions = golden_regions(cond)
    if regions:
        got = response_regions(res)
        missed = [r for r in regions if _norm_region(r) not in got]
        if missed:
            wrong.append(f"지역 {'·'.join(missed)} 없음")
    want, got_years = golden_years(cond), response_years(res)
    # 정답 기간의 처음·끝 연도가 답에 있으면 맞음. 답변 첫 문단에 "자료는 2008~2025년만 있습니다"
    # 같은 수록 기간 안내가 섞여도 범위를 넓게 잡지 않게 연도 하나하나로 본다
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
        # 숫자 검사는 답변 어디에 있든 맞다고 보므로, 남녀 내역을 같이 보여 준 답이
        # 반대 성별로 답해도 정답이 되지 않게 성별을 따로 본다
        wrong.append(f"성별 {sex or '없음'} (정답 {want_sex})")
    return wrong


SEX_WORDS = {"남": re.compile(r"남자|남성|남편"), "여": re.compile(r"여자|여성|아내")}


def golden_sex(cond: str) -> str | None:
    """정답 조건이 한 성별만 물으면 "남"/"여". 두 성별이 다 나오면(비교, "남편·아내 총계",
    "한국인 남편+외국인 아내") 성별로 가르지 않는 것이라 None"""
    found = [s for s, pattern in SEX_WORDS.items() if pattern.search(cond)]
    return found[0] if len(found) == 1 else None


# ---- 출처 ---------------------------------------------------------------------------------


def golden_tables(cell: str) -> tuple[list[str], list[str]]:
    """정답 표 칸 → (정답 표 ID들, 허용 표 ID들). 첫 줄이 정답 표, "허용:" 줄이 허용 표"""
    main, allow = [], []
    for line in cell.splitlines():
        ids = TABLE_ID.findall(line)
        (allow if line.strip().startswith("허용") else main).extend(ids)
    return main, allow


def check_sources(cell: str, res: dict) -> tuple[list[str], list[str]]:
    """(빠진 출처 요소, 표 문제). 4요소: 통계표명, 작성기관, 기준 시점, kosis 링크"""
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


# ---- 환각 ---------------------------------------------------------------------------------

# 원인·예측·정책 평가로 읽히는 표현. 걸린 문장은 "환각 문장 후보"로 사람이 확인한다
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
    """답변 속 숫자 중 계산 결과·조건·출처·통계표 안내에 없는 숫자.
    연도, 순위 번호("1위"), 질문에 나온 숫자, 계산 결과 두 값의 차이는 세지 않는다"""
    allowed: list[float] = []
    for key in ("result", "parsed", "applied", "sources", "chart", "tables"):
        _leaf_numbers(res.get(key), allowed)
    _leaf_numbers(question, allowed)
    # 계산 결과 두 값의 차이 ("남편 33.85세, 아내 31.62세 … 2.23세 더 높습니다")
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
        # 계산 결과를 %로 바꿔 쓴 경우 (0.212 → 21.2%)
        if any(abs(abs(n.value) - abs(a) * 100) <= n.tol + 1e-9 for a in allowed):
            continue
        out.append(n.raw)
    return out


def claim_sentences(answer: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?。])\s+|\n", answer) if CLAIM.search(s)]


# ---- 한 문항 채점 ---------------------------------------------------------------------------


@dataclass
class Grade:
    id: str
    set_name: str
    expected: str
    actual: str
    action_error: str = ""
    number: str = ""  # O / X / "" (해당 없음)
    condition: str = ""
    source: str = ""
    refusal: str = ""
    hallucination: int = 0
    correct: str = "X"
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


SETS = {"D": "개발셋", "T": "테스트셋", "N": "신규 테스트셋", "M": "멀티턴 골든셋"}


def grade(row: dict[str, str], first: dict, final: dict | None = None) -> Grade:
    """row: 골든셋 한 줄, first: 첫 질문의 응답,
    final: 되묻기 뒤 사용자 답을 넣은 응답 (없으면 first)"""
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

    # 답변·되묻기 문항: 최종 답을 채점한다 (되묻기 문항은 되물은 뒤 사용자 답까지 넣은 답)
    answered = action_of(final) == ANSWER
    text = final.get("answer") or ""
    exp = expected_numbers(row["질문 종류"], row["정답 수치"])
    nums = numbers(text)
    found = {e.label: find_number(e, nums, text) for e in exp}
    for alt in allowed_answers(row["정답 수치"]):
        # 정답 줄로 다 맞지 않으면 허용 줄로 본다 (허용 줄 하나를 통째로 맞아야 한다)
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
        # 허용 줄이 시점을 적었으면 (예: "… 37.66명 (2026년 10월 말)") 기간은 그 시점으로 본다
        lo, hi = golden_years(alt)
        cond = YEAR.sub("", cond) + (f", {lo}" if lo == hi else f", {lo}~{hi}")
    cond_wrong = check_conditions(cond, final) if answered else ["답하지 않음"]
    g.details["조건 차이"] = cond_wrong
    g.condition = "X" if cond_wrong else "O"

    missing, table_issue = check_sources(row["정답 표"], final) if answered else (["출처 없음"], [])
    g.details["빠진 출처"] = missing
    g.details["표 차이"] = table_issue
    if table_issue and g.number == "O" and g.condition == "O":
        # 정답 인정 기준: 정답 표와 달라도 같은 숫자를 맞게 전달했으면 정답 (표 차이는 기록만)
        g.details["표 차이"] = [f"{t} (숫자가 맞아 인정)" for t in table_issue]
        table_issue = []
    g.source = "X" if missing or table_issue else "O"

    ok = not g.action_error and g.number == "O" and g.condition == "O" and g.source == "O"
    g.correct = "O" if ok else "X"
    if not ok:
        g.reason = _first_wrong(g, answered, table_issue, cond_wrong, in_result, claims)
    return g


def _first_wrong(g, answered, table_issue, cond_wrong, in_result, claims) -> str:
    """처음 어긋난 단계 하나 (행동 → 표 → 조건 → 계산 → 요약)"""
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
    return "요약"  # 계산 결과에는 맞는 숫자가 있는데 답변 문장·출처가 틀림


def _grade_refusal(g: Grade, row: dict[str, str], res: dict) -> Grade:
    status = res.get("status")
    tables = res.get("tables") or {}
    # 통계표 찾기가 범위 밖으로 거절한 답은 바깥 status가 "tables"라 안쪽 status를 본다
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
