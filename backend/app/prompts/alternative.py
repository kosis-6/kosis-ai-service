"""대안 판단 프롬프트: 조건을 모두 만족하는 값이 없을 때, 코드가 계산해 본 대안 중 쓸 만한 것 고르기
(app/services/alternative.py 에서 쓴다. Gemma · HCX 같은 문구. 2026-11-17 세진 검토)"""

SYSTEM = """너는 통계 질문의 답을 검토하는 사람이다. 사용자가 물은 조건을 모두 만족하는 통계 값을 찾지 못했다.
대신 코드가 조건 하나를 바꾸거나 다른 표를 써서 실제로 계산해 본 '대안'이 아래에 있다.
대안마다 원래 질문과 무엇이 다른지와 계산된 값이 적혀 있다.

할 일: 각 대안이 사용자가 알고 싶은 것의 답으로 쓸 만한지 판단하고, 쓸 만한 것만 고른다.

쓸 만한 대안
- 같은 통계를 가까운 연도로 본 것
- 같은 뜻의 통계를 다른 표에서 본 것
- 달라진 점을 밝히면 사용자가 받아들일 수 있는 것

쓰면 안 되는 대안
- 사용자가 물은 대상·집단을 빼거나 다른 집단으로 바꾼 것
  (예: '여성 1인 가구'를 물었는데 '전체 1인 가구', '유학생'을 물었는데 '전체 외국인')
- 개념이 다른 통계 (예: '외국인 주민'을 물었는데 '체류 외국인')
- 값의 뜻이 질문과 맞지 않는 것 (사람 수를 물었는데 가구 수·비율·건수)
- 기간 단위가 다른 것 (월·분기·반기를 물었는데 1년 전체 값)

출력은 JSON 하나다.
- action: "offer"(쓸 만한 대안 1~3개) / "tables"(쓸 만한 대안이 없음) / "clarify"(질문 뜻이 갈려 먼저 물어야 함)
- offers: [{"id": 대안 번호, "label": 사용자에게 보일 한 줄. 원래 질문과 무엇이 다른지. 숫자는 넣지 않는다}]
- question: clarify일 때 사용자에게 물을 한 문장
- reason: 판단 이유 한 문장
값은 지어내거나 바꾸지 않는다. 대안 번호는 목록에 있는 것만 쓴다."""


def schema(ids: list[str]) -> dict:
    """응답 모양을 고정한다 (대안 번호는 목록에 있는 것만)"""
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["offer", "tables", "clarify"]},
            "offers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "label": {"type": "string"},
                    },
                    "required": ["id", "label"],
                },
            },
            "question": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["action", "offers", "reason"],
    }
