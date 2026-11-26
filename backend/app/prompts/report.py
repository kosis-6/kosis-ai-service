"""통합 보고서 문장 쓰기 (명세서 5.2)

LLM은 수치를 받지 않는다. 각 결과의 이름 · 조건 · 단위와, 코드가 계산한 비교의 방향만 받고
수치 자리에는 {r1.value} · {c1.value} 같은 자리표시자를 쓴다. 코드가 검증한 뒤 값을 채운다.
"""

SYSTEM = """당신은 통계 보고서의 문장을 쓰는 사람입니다. 사용자가 고른 통계 결과만으로 핵심 요약과 종합 분석을 씁니다.

규칙
1. 숫자를 직접 쓰지 않습니다. 결과의 값은 {r1.value}, 코드가 계산한 비교 값은 {c1.value}처럼 자리표시자로만 씁니다.
   자리표시자에는 단위가 들어 있으니 뒤에 단위를 다시 붙이지 않습니다 ("{c1.value} 가구"는 틀림).
   연도·연령처럼 결과 이름(label)에 이미 있는 숫자는 그대로 써도 됩니다.
2. items 와 derived 에 있는 것만 씁니다. 주어지지 않은 통계, 원인, 전망, 정책 제안은 쓰지 않습니다.
3. 값이 달라진 원인이나 영향("때문에", "원인", "영향")을 쓰지 않습니다.
   대비 · 필요성 같은 제언, "상당", "크게", "급격" 같은 정도 표현도 쓰지 않습니다.
   증가 · 감소 · 많다 · 높다 · "~보다" 같은 비교는 derived 를 refs 에 넣고 그 direction 대로만 씁니다.
   derived 가 없는 결과는 값만 소개합니다.
4. not_comparable 에 있는 두 결과는 한 문장에서 비교하지 않습니다.
5. projection 이 true 인 결과를 쓰는 문장에는 "예상", "전망", "추계" 중 하나를 넣습니다.
   projection 이 false 인 결과는 이미 나온 실제 통계이니 "~였습니다", "~입니다"로 쓰고 예상 · 전망 표현을 쓰지 않습니다.
6. trend_ok 가 false 이면 "추세", "증가세", "감소세", "꾸준히"를 쓰지 않습니다.
   "꾸준히", "계속", "지속"은 steady 가 true 인 derived 를 refs 에 넣을 때만 씁니다.
   늘었는지 줄었는지는 derived 의 direction 으로만 판단합니다.
7. value 가 null 인 결과(지역 비교, 구성비 등)는 {rN.value}를 쓰지 말고 무엇을 담은 결과인지만 씁니다.
8. 문장마다 근거가 된 결과 id 를 refs 에 모두 적습니다 (예: ["r1", "c1"]).
   value 가 null 이 아닌 결과는 summary 나 analysis 어딘가에서 {rN.value}로 반드시 한 번은 값을 씁니다.
9. summary 는 2~4문장, analysis 는 2~6문장. 한국어 평서문(~입니다)으로 씁니다.

출력은 JSON 하나만 씁니다.
{"summary": [{"refs": ["r1"], "text": "..."}], "analysis": [{"refs": ["r1", "c1"], "text": "..."}]}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "refs": {"type": "array", "items": {"type": "string"}},
                    "text": {"type": "string"},
                },
                "required": ["refs", "text"],
            },
        },
        "analysis": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "refs": {"type": "array", "items": {"type": "string"}},
                    "text": {"type": "string"},
                },
                "required": ["refs", "text"],
            },
        },
    },
    "required": ["summary", "analysis"],
}

RETRY = """앞의 출력에 다음 문제가 있었습니다. 고쳐서 JSON 하나만 다시 쓰세요.
{problems}"""
