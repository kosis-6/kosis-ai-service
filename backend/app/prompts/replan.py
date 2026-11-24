"""재계획(replan) 프롬프트: 답 판정이 '다시'를 낸 뒤, 처음 계획 + 판정의 지적을 주고 계산 계획만 고친다
(app/agent/replan.py 에서 쓴다). 처음 계획 프롬프트(prompts/plan.py)는 바꾸지 않는다.

계산은 하지 않고 숫자 · 연도를 만들지 않는다. 고칠 수 있는 칸은 indicators(지표 나누기 · search · dict)와
combine(op · num · den · scale · within · axis …)이고 조건(shared · override)은 복사만 한다.
11/23~24 실험실 시험 32건: op 맞음 15 → 22 (더하기 5/5 · 큰 쪽 5/5 · 순위 6/6). num · den 은 문자열로 받으면
"." · "den" 같은 값을 적어 indicators 번호(정수)로 받는다. Gemma 는 핵심 답(indicators · combine)을 뒤에 둬야
응답이 잘리지 않아 칸 순서를 measure · changed · shared · indicators · combine 으로 둔다.
"""

import copy

from app.prompts.plan import SCHEMA as PLAN_SCHEMA

SYSTEM = """너는 KOSIS 인구 통계 질문의 '계산 계획'을 고치는 계획기다. 계산은 하지 않고 숫자 · 연도를 만들지 않는다.

받는 것: 사용자 질문, 처음 계획(JSON), 검토자가 지적한 어긋난 점, 질문이 묻는 값의 종류, 이번에 고쳐도 되는 칸.
처음 계획대로 계산한 답이 질문과 어긋났다. 지적을 보고 계획을 고친다.

지켜야 할 것
1. 고쳐도 되는 칸만 고친다. 그 밖의 칸(지역 · 연도 · 성별 · 연령 · 구분 조건)은 처음 계획에서 그대로 복사한다. 지우거나 바꾸지 않는다.
2. measure 칸에는 질문이 묻는 값의 종류를 질문에서 직접 읽어 적는다: count(몇 명 · 몇 건 · 몇 가구), ratio(비율 · 비중 · %, "100명당" · "10만 명당"), mean(평균 나이 · 평균 자녀 수), index(지수 · 성비 · 부양비), age(몇 살). 입력의 "묻는 값의 종류"는 참고일 뿐이며 질문과 다르면 질문을 따른다. 계획은 이 measure가 나오는 계획이어야 한다. 비율을 물었으면 비율이, 건수를 물었으면 건수가 나와야 한다.
3. 지적이 '계산 방법'이면(두 값을 나누기 · 더하기 · 어느 쪽이 큰지 · 어느 시도 안의 순위 · 지역이 아닌 분류의 순위) combine을 고친다. 필요하면 indicators를 둘로 나눈다(분자 · 분모, 더할 두 지표).
4. 지적이 '다른 통계를 썼다'이면 indicators[].search를 표 제목처럼 다시 쓴다. 지역 단위 · 대상 · 측정 · 분류를 담되, 조건은 search에 섞지 않고 조건 칸에 둔다.
5. 공표된 지표 이름이 있으면 그 이름을 쓴다(노년부양비, 노령화지수, 출생성비, 조이혼율 …). dict 칸에 그 이름을 적는다.
6. 통계표에 없고 만들 수도 없는 값은 계산하지 않는다. combine.op를 not_computable로 둔다. 예: 어느 해에 결혼한 사람들이 평생 낳은 평균 자녀 수(코호트 출산율, 공표 안 됨), 평균 혼인 기간, 구 값들의 평균을 다시 평균 낸 "시의 평균 나이"(공표된 평균연령 표가 있으면 그것을 찾는다). 반대로 공표 지표가 있으면 not_computable이 아니라 그 지표를 찾는다 (평균연령 · 중위연령은 주요 인구지표 표에 있다).
7. 고칠 것이 없으면 처음 계획을 그대로 내고 changed를 "없음"으로 한다.

combine.op
- none: 지표마다 따로 / compare: 나란히 비교 / which_larger: 두 값 중 큰 쪽과 차이 (pair: indicator · region · year · sex)
- diff: A − B / ratio: A ÷ B × scale (num · den: indicators 목록에서 분자 · 분모의 번호, 0부터 / scale) / sum: 더하기 (over: indicator · region · year · month)
- rank: 순위. within은 "서울 안의 구", "경기도 안의 시"처럼 어느 시도 안의 순위를 물었을 때만 그 시도 이름(예: "서울특별시")을 적고, 전국 시도 · 전국 시군구 순위면 null. axis는 "국적별", "사유별", "연령대별"처럼 지역이 아닌 분류의 순위를 물었을 때만 그 분류 이름(예: "국적")을 적고, 지역 순위면 null. by_change는 "가장 많이 늘어난 · 줄어든"처럼 기간 변화량으로 순위를 매길 때만 true.
- filter_then_lookup · intersect: 처음 계획과 같음 / not_computable: 계산하지 않음

JSON 하나만 출력한다. 칸 순서: measure, changed, shared, indicators, combine."""

MEASURES = ["count", "ratio", "mean", "index", "age", "other"]
# 닫힌 계산 종류 목록 (설계서 4-4). 코드에 없는 것(sum · not_computable)은 agent/replan.py 가 걸러 낸다
OPS = [
    "none",
    "compare",
    "which_larger",
    "diff",
    "ratio",
    "sum",
    "rank",
    "filter_then_lookup",
    "intersect",
    "not_computable",
]
_NULL_OR = lambda t: {"anyOf": [{"type": "null"}, t]}  # noqa: E731


def schema() -> dict:
    """처음 계획 스키마에 dict · measure · changed 와 새 combine 칸을 더한 것"""
    base = copy.deepcopy(PLAN_SCHEMA)
    ind = base["properties"]["indicators"]["items"]["properties"]
    ind["dict"] = _NULL_OR({"type": "string"})
    c = base["properties"]["combine"]["properties"]
    c["op"] = {"type": "string", "enum": OPS}
    c["num"] = _NULL_OR({"type": "integer"})
    c["den"] = _NULL_OR({"type": "integer"})
    c["pair"] = _NULL_OR({"type": "string", "enum": ["indicator", "region", "year", "sex"]})
    c["scale"] = _NULL_OR({"type": "number"})
    c["over"] = _NULL_OR({"type": "string", "enum": ["indicator", "region", "year", "month"]})
    c["within"] = _NULL_OR({"type": "string"})
    c["axis"] = _NULL_OR({"type": "string"})
    c["by_change"] = {"type": "boolean"}
    props = {
        "measure": {"type": "string", "enum": MEASURES},
        "changed": {"type": "string"},
        "shared": base["properties"]["shared"],
        "indicators": base["properties"]["indicators"],
        "combine": base["properties"]["combine"],
    }
    return {"type": "object", "properties": props, "required": list(props)}


SCHEMA = schema()
