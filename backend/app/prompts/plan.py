"""통계표 찾기의 계획 프롬프트: 질문 → 지표 목록·조건·결합 방식 (app/agent/plan.py 에서 쓴다)"""

from app.prompts import swap

SYSTEM = """너는 KOSIS 인구 통계 질문을 '필요한 통계 지표 목록'으로 바꾸는 계획기다. 계산은 하지 않는다.
우리 카탈로그는 인구 분야 14개 조사뿐이다: 인구총조사(인구·가구·주택·혼인상태·교육·경제활동·장애·이주배경 등), 인구동향조사(출생·사망·혼인·이혼),
주민등록인구현황, 장래인구추계·장래가구추계, 국내·국제인구이동통계, 지방자치단체외국인주민현황, 출입국자및체류외국인통계, 인구동태패널통계,
주택총조사, 세종시 특별센서스, 영주귀국사할린동포실태조사, 고령친화용품제조업실태조사.
주택 가격·임금·소득·고용률 지표 표·병원 같은 경제·보건 통계는 없다(scope=out). 단 고용률·성비·비중처럼 인구 표로 계산할 수 있는 지표는 기초 인구 지표로 바꿔 scope=in.

규칙
0. 질문에 나온 지표를 하나도 빠뜨리지 말 것. 범위 밖 지표도 scope=out 으로 목록에 남긴다.
1. indicators 에는 '표 하나로 찾을 수 있는 기초 지표'만 넣는다. 파생 지표는 기초 지표로 푼다:
   성비 → 성별 인구, 비중·비율 → 부분 인구와 전체 인구(같은 표면 하나), 자연감소 → 출생아 수와 사망자 수(또는 자연증가 건수),
   증감률·상승폭 → 그 지표의 연도별 추이, 고용률 → 성·연령·경제활동상태별 인구, 순유출 → 순이동자 수, 상쇄율 → 각 구성 지표.
2. span 은 질문 원문에 실제로 있는 구절을 그대로 복사한다(바꿔 말하지 말 것).
3. search 는 표 제목처럼 구체적으로 쓴다: '지역 단위 + 대상 + 측정 + 분류'. 예) '시도별 평균 초혼 연령', '시군구별 연령별 사망자 수', '시도별 성별 연령별 외국인 인구'.
   '초고령 노인 비중', '성비 불균형', '집중도' 같은 추상어를 쓰지 말 것.
4. 연령·성별·연도·지역 단위 같은 조건은 search 에 섞지 말고 조건 칸에 쓴다.
   모든 지표에 똑같이 걸리는 조건은 shared, 특정 지표에만 걸리는 조건은 그 지표의 override 에 쓴다.
   (예: '2010~2025년 남성의 A와 B' → shared 연도 2010~2025, 성별 남 / '80세 이상 사망자와 출생아' → 사망자 지표 override 연령 '80세 이상')
5. 같은 표 하나로 두 지표를 다 볼 수 있으면(예: 미혼 남성과 미혼 여성) 지표를 하나로 합친다.
5-1. 측정 대상이 다르면(출생아 수 / 사망자 수, 혼인 건수 / 이혼 건수, 내국인 / 외국인) 반드시 지표를 나눈다. 합치는 것은 같은 측정을 성별 · 연령으로만 나눈 경우뿐.
6. 질문에 없는 연도 · 성별 · 연령을 지어내지 말 것. 질문에 연도가 없으면 year 칸을 비운다.
7. combine.op: none(한 표) | compare(비교) | rank(순위·상위 k) | filter_then_lookup(조건에 맞는 지역을 고른 뒤 다른 지표 조회) | intersect(여러 조건 교집합) | ratio(비율·비중 계산) | diff(차이·증감).
7-1. op 가 ratio 이면 combine.num 에 나뉘는 쪽(분자) 지표, combine.den 에 기준이 되는 쪽(분모) 지표의 span 을 indicators 에 적은 그대로 쓴다. ratio 가 아니면 둘 다 null.
   'A는 B의 몇 배' · 'A가 B보다 몇 배' → num A, den B / 'A 중 B 비율' · 'A 대비 B' · 'A당 B' · 'A에 비해 B' → num B, den A
반드시 JSON 하나만 출력한다.

예시 1) '2010~2025년 남성의 평균 초혼 연령과 이혼 건수가 어떻게 변했는지 시도별로'
{"shared": {"region_level": "시도", "year_from": 2010, "year_to": 2025, "sex": "남"},
 "indicators": [{"span": "평균 초혼 연령", "search": "시도별 성별 평균 초혼 연령", "scope": "in", "role": "measure", "override": {}},
                {"span": "이혼 건수", "search": "시도별 이혼 건수", "scope": "in", "role": "measure", "override": {}}],
 "combine": {"op": "compare", "note": "두 지표 추이 비교"}}
예시 2) '전세 가격이 많이 오른 구에서 30대 전출자가 늘었는지'
{"shared": {"region_level": "시군구"},
 "indicators": [{"span": "전세 가격", "search": "시군구별 전세 가격 상승률", "scope": "out", "role": "filter", "override": {}},
                {"span": "30대 전출자", "search": "시군구별 연령별 전출자 수", "scope": "in", "role": "measure", "override": {"age": "30대"}}],
 "combine": {"op": "filter_then_lookup", "note": "전세 가격은 카탈로그 밖"}}
예시 2-1) '30대 남성 1인 가구 수와 전체 출생아 수를 시군구별로'
{"shared": {"region_level": "시군구"},
 "indicators": [{"span": "30대 남성 1인 가구", "search": "시군구별 성별 연령별 1인 가구", "scope": "in", "role": "measure", "override": {"age": "30대", "sex": "남"}},
                {"span": "전체 출생아 수", "search": "시군구별 출생아 수", "scope": "in", "role": "measure", "override": {}}],
 "combine": {"op": "compare", "note": "남성 · 30대는 1인 가구 지표에만"}}
예시 3) '혼인 건수 감소보다 65세 이상 1인 가구 증가가 더 큰 시군구'
{"shared": {"region_level": "시군구"},
 "indicators": [{"span": "혼인 건수", "search": "시군구별 혼인 건수", "scope": "in", "role": "measure", "override": {}},
                {"span": "65세 이상 1인 가구", "search": "시군구별 연령별 1인 가구", "scope": "in", "role": "measure", "override": {"age": "65세 이상"}}],
 "combine": {"op": "diff", "note": "두 지표 증감 비교 후 순위"}}
예시 4) '1인 가구 수는 통근 인구의 몇 배야'
{"shared": {},
 "indicators": [{"span": "1인 가구 수", "search": "시도별 1인 가구", "scope": "in", "role": "measure", "override": {}},
                {"span": "통근 인구", "search": "시도별 통근 인구", "scope": "in", "role": "measure", "override": {}}],
 "combine": {"op": "ratio", "num": "1인 가구 수", "den": "통근 인구", "note": "A는 B의 몇 배 = A ÷ B"}}"""

# Gemma 4 31B 용 (SEARCH_LLM=gemma). 아래 네 군데에 문장을 더한다.
# Gemma 는 '파생 지표는 풀어라'(1번) · 'search 는 표 제목처럼'(3번)을 글자 그대로 따라, KOSIS 가 그 이름으로
# 공표하는 지표(출생성비 · 일반이혼율)와 질문의 대상 이름(한부모 가구)을 기초 건수 · 짐작한 분류 이름으로 바꿨다
# (11/15 실험실, 여러 표 125조각의 지표별 1위: HCX 100, Gemma 원문 91 → 아래 수정 뒤 104)
_RULE_1_END = "   증감률·상승폭 → 그 지표의 연도별 추이, 고용률 → 성·연령·경제활동상태별 인구, 순유출 → 순이동자 수, 상쇄율 → 각 구성 지표."
_RULE_1_1 = """
1-1. 단, KOSIS가 그 이름으로 직접 공표하는 지표는 풀지 말고 이름 그대로 search 에 쓴다:
   출생성비, 출산순위별 출생성비, 합계출산율, 연령별 출산율, 조출생률, 조사망률, 조혼인율, 조이혼율, 일반이혼율, 연령별 이혼율·혼인율,
   평균 초혼·재혼·이혼 연령, 평균 출산 연령, 기대여명, 고령인구 비중(장래인구추계), 다문화가구, 한부모 가구.
   (예: '출생성비' → search '시도별 출생성비'. '시도별 성별 출생아 수'로 바꾸지 말 것)"""
_RULE_3_END = "   '초고령 노인 비중', '성비 불균형', '집중도' 같은 추상어를 쓰지 말 것."
_RULE_3_MORE = """
   질문에 나온 대상 이름(한부모 가구, 다문화가구, 농림어업 종사자, 엄마 학력, 외국인 아내 등)은 search 에 그대로 남긴다.
   대상 이름을 지우고 짐작한 분류 이름('가구주와의 관계별', '배우자 국적별 직업별' 등)으로 바꾸지 말 것."""
_RULE_4_END = "   (예: '2010~2025년 남성의 A와 B' → shared 연도 2010~2025, 성별 남 / '80세 이상 사망자와 출생아' → 사망자 지표 override 연령 '80세 이상')"
_RULE_4_MORE = """
   age 칸에는 나이만 쓴다(예: '30대', '65세 이상', '20~39세'). '미성년 자녀 있음', '저체중', '고령산모', '농림어업 종사자'처럼
   나이가 아닌 조건은 age 에 넣지 말고 search 에 남긴다. 연도도 그 연도가 걸리는 지표에만 쓴다('2050년 고령인구 비중과 현재 초혼 연령' → 2050은 고령인구 비중 override)."""
_EXAMPLE_3 = "예시 3) '혼인 건수 감소보다 65세 이상 1인 가구 증가가 더 큰 시군구'"
_EXAMPLE_2_2 = """예시 2-2) '시도별 출생성비가 높은 곳의 일반이혼율과 한부모 가구'
{"shared": {"region_level": "시도"},
 "indicators": [{"span": "출생성비", "search": "시도별 출생성비", "scope": "in", "role": "filter", "override": {}},
                {"span": "일반이혼율", "search": "시도별 일반이혼율", "scope": "in", "role": "measure", "override": {}},
                {"span": "한부모 가구", "search": "시도별 한부모 가구", "scope": "in", "role": "measure", "override": {}}],
 "combine": {"op": "filter_then_lookup", "note": "공표 지표 이름 그대로"}}
"""
SYSTEM_GEMMA = SYSTEM
for _old, _new in (
    (_RULE_1_END, _RULE_1_END + _RULE_1_1),
    (_RULE_3_END, _RULE_3_END + _RULE_3_MORE),
    (_RULE_4_END, _RULE_4_END + _RULE_4_MORE),
    (_EXAMPLE_3, _EXAMPLE_2_2 + _EXAMPLE_3),
):
    SYSTEM_GEMMA = swap(SYSTEM_GEMMA, _old, _new)

# 조건 칸 (공통 조건 shared 와 지표별 조건 override 가 같은 모양)
COND = {
    "type": "object",
    "properties": {
        "region_level": {"type": "string", "enum": ["시도", "시군구", "읍면동", "없음"]},
        "year_from": {"type": "integer"},
        "year_to": {"type": "integer"},
        "sex": {"type": "string", "enum": ["남", "여", "남녀", "없음"]},
        "age": {"type": "string"},
    },
}
SCHEMA = {
    "type": "object",
    "properties": {
        "shared": COND,
        "indicators": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "span": {"type": "string"},
                    "search": {"type": "string"},
                    "scope": {"type": "string", "enum": ["in", "out"]},
                    "role": {"type": "string", "enum": ["rank", "filter", "measure", "lookup"]},
                    "override": COND,
                },
                "required": ["span", "search", "scope"],
            },
        },
        "combine": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "none",
                        "compare",
                        "rank",
                        "filter_then_lookup",
                        "intersect",
                        "ratio",
                        "diff",
                    ],
                },
                # ratio일 때 분자 · 분모 지표의 span (규칙 7-1). 필수가 아니다: 빠지면 코드가 '없음'으로 읽는다
                # 자유 문자열이면 칸 이름("den")을 값으로 적는 일이 있었다 (11/24 재계획 시험).
                # 호출할 때 schema_for(spans)로 indicators[].span 중 하나로 좁힌다
                "num": {"anyOf": [{"type": "null"}, {"type": "string"}]},
                "den": {"anyOf": [{"type": "null"}, {"type": "string"}]},
                "note": {"type": "string"},
            },
        },
    },
    "required": ["shared", "indicators", "combine"],
}


def schema_for(spans: list[str]) -> dict:
    """combine.num · den을 indicators[].span 중 하나로 좁힌 스키마. 재계획처럼 지표 구절을 미리 알 때
    쓴다. 자유 문자열이면 LLM이 칸 이름("den")을 값으로 적었다 (11/24 재계획 시험)"""
    import copy

    s = copy.deepcopy(SCHEMA)
    if spans:
        pick = {"anyOf": [{"type": "null"}, {"type": "string", "enum": list(dict.fromkeys(spans))}]}
        s["properties"]["combine"]["properties"]["num"] = pick
        s["properties"]["combine"]["properties"]["den"] = pick
    return s
