from datetime import date

from app.prompts.qa import PARSE_SYSTEM
from app.prompts.search import SLOT_SYSTEM_GEMMA


def _section(text: str, start: str, end: str | None = None) -> str:
    s = text.index(start) + len(start)
    e = text.index(end, s) if end else len(text)
    return text[s:e].strip()


def _drop_rules(rules: str, prefixes: tuple[str, ...]) -> str:
    out, skip = [], False
    for line in rules.splitlines():
        if line.startswith("- "):
            skip = line[2:].startswith(prefixes)
        elif not line.startswith(" "):
            skip = False
        if not skip:
            out.append(line)
    return "\n".join(out)


def _common_rules(today: date, latest: int) -> str:
    y = today.year
    return f"""- regions: 질문에 나온 지역 표현 그대로, 여럿이면 모두 (예: ["서울"], ["서울", "부산"], ["경기 수원시"], ["우리 구"]). 없으면 [].
- region_level: 통계표를 지역별로 나눠 보려는 단위. "시도별"이면 "시도", "시군구별"·"구별"이면 "시군구", "읍면동별"이면 "읍면동". 없으면 null.
- year, year_end: 숫자로 말한 연도는 그대로. 기간이면 year=시작, year_end=끝. 상대 표현은 숫자로 바꾼다.
  · "작년" · "재작년" · "올해" · "N년 뒤"는 올해({y}) 기준 (작년 = {y - 1}, 3년 뒤 = {y + 3})
  · "N년 전보다" · "N년 동안" · "지금까지" · "최근 N년"은 끝이 자료 마지막 해({latest})
    (10년 전보다 → year {latest - 10}, year_end {latest} / 2015년부터 지금까지 → 2015, {latest} / 최근 5년 → {latest - 4}, {latest})
  · 연도를 아예 말하지 않았으면 둘 다 null
- sex: 질문이 한 성별만 말하면 "남" 또는 "여", 남녀 합계를 명시하면 "전체". 성별 말이 없으면 null.
  "성별 인구", "남녀별"처럼 남녀로 나눠 보려는 경우는 sex는 null로 두고 search.classes에 "성별"을 넣는다."""  # noqa: E501


def system(indicators: str, today: date, latest: int) -> str:
    num_types = _section(
        PARSE_SYSTEM, "답하거나 계산하지 말고 JSON 하나만 출력하세요.", "JSON 형식"
    )
    num_rules = _drop_rules(
        _section(PARSE_SYSTEM, "규칙\n"), ("regions:", "sex:", "연도가 없으면")
    ).replace("{INDICATORS}", indicators)
    slot_rules = _section(SLOT_SYSTEM_GEMMA, "규칙\n", "\n예\n")
    slot_rules = _drop_rules(slot_rules, ("region:", "region_level:", "year:", "sex:"))
    return f"""당신은 인구통계 질문을 분석해 JSON 하나로 바꾸는 역할입니다. 답하거나 계산하거나 통계표를 고르지 말고 JSON 하나만 출력하세요.
오늘: {today.isoformat()} (올해 {today.year}) · 자료 마지막 해: {latest}
같은 질문을 한 번 읽어 세 블록에 적습니다.
- "common": 지역 · 연도 · 성별 · 나눠 보는 지역 단위 (두 용도가 함께 쓴다)
- "search": 통계표를 찾기 위한 해석
- "numeric": 수치를 계산하기 위한 해석
질문에 없는 값은 추측하지 말고 null 또는 []로 둡니다. 빈 칸을 0, "", 공백으로 채우지 않습니다.
numeric 블록에서 값이 null · false · []인 칸(sido, top, ascending, by, level)은 쓰지 않아도 된다. type · indicator · term은 늘 쓴다. term은 질문의 지표 표현만 짧게.
JSON 하나만 쓰고, JSON 뒤에 설명을 덧붙이지 않습니다.

JSON 형식 (블록 순서 그대로: common, search, numeric)
{{"common": {{"regions": [], "region_level": null, "year": null, "year_end": null, "sex": null}},
 "search": {{"topic": "...", "search_query": "...", "target": null, "exclude": [], "classes": [], "items": [], "query_type": "find_table"}},
 "numeric": {{"type": "...", "indicator": "...", "term": "...", "level": "시도", "sido": null, "top": null, "ascending": false, "by": null}}}}

[common 블록 규칙]
{_common_rules(today, latest)}

[search 블록 규칙]
{slot_rules}

[numeric 블록: 질문 유형(type)]
{num_types}

[numeric 블록 규칙]
{num_rules}

예
질문: 2019년 광주 동구에 사는 70대 여자 몇 명이야?
{{"common": {{"regions": ["광주 동구"], "region_level": null, "year": 2019, "year_end": null, "sex": "여"}},
 "search": {{"topic": "인구 수", "search_query": "성 및 연령별 인구", "target": null, "exclude": [], "classes": [], "items": ["70대"], "query_type": "find_table"}},
 "numeric": {{"type": "value", "indicator": "총인구", "term": "70대 여자"}}}}"""  # noqa: E501
