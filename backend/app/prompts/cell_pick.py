"""칸 고르기 프롬프트: 규칙(글자 맞추기)이 칸을 못 고른 표에서 질문에 맞는 칸 고르기
(app/services/cell_pick.py 에서 쓴다. Gemma · HCX 같은 문구. 2026-11-18 세진 검토)"""

UNSURE = "정할 수 없음"
YES, NO = "예", "아니오"

SYSTEM = """너는 통계표에서 질문에 맞는 칸을 고르는 도우미다.
질문, 질문 해석, 통계표 이름, 분류별 칸 목록을 받는다.
지역 · 연령 · 성별 · 연도는 코드가 이미 골랐으니 목록에 없다.

할 일
1. 이 표가 질문이 묻는 값을 담고 있는지 판단한다 (fits).
   - 낱말이 달라도 뜻이 같으면 담고 있는 것이다. (예: 결혼 건수 = 혼인, 아기가 태어난 수 = 출생아 수)
   - 질문이 묻는 대상이나 값이 표에 없으면 "아니오"다.
2. 담고 있으면 분류마다 칸 번호를 하나 고른다.
   - 질문이 그 분류의 특정 칸을 말하면 그 칸을 고른다.
   - 질문이 그 분류를 말하지 않으면 합계 칸(계 · 총계 · 전체)을 고른다. 합계 칸이 없으면 "정할 수 없음".
   - 질문과 뜻이 다르거나 더 넓은 칸으로 대신하지 않는다. 맞는 칸이 없으면 fits를 "아니오"로 한다.
   - 두 칸 이상이 똑같이 맞으면 "정할 수 없음"으로 둔다 (사용자에게 묻는다).
3. 질문의 낱말을 표의 다른 낱말로 바꿔 읽었으면 read_as에 적는다.
4. 목록에 없는 번호는 쓰지 않는다. 값(숫자)은 추측하지 않는다."""


def schema(groups: dict[str, int]) -> dict:
    """응답 모양을 고정한다. groups: {분류 이름: 칸 수}. 칸 번호는 목록에 있는 것만"""
    # 칸 순서: picks를 맨 뒤에 둔다. 모델은 핵심 답(picks)을 쓰고 나면 JSON을 닫고 싶어 하는데, 그 뒤에
    # 필수 칸(reason)이 남아 있으면 닫는 글자가 막혀 공백만 이어 쓰다 글자 수 한도에 잘렸다
    # (11/18 Gemma: fits → picks → read_as → reason 순서는 세 표 모두 잘림, fits → reason → read_as →
    # picks는 정상. reason을 맨 앞에 두면 이유부터 쓰다 "연도 분류가 없다"며 아니오로 기울었다)
    return {
        "type": "object",
        "properties": {
            "fits": {"type": "string", "enum": [YES, NO]},
            "reason": {"type": "string"},
            "read_as": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"질문": {"type": "string"}, "표": {"type": "string"}},
                    "required": ["질문", "표"],
                },
            },
            "picks": {
                "type": "object",
                "properties": {
                    name: {"type": "string", "enum": [*(str(i) for i in range(1, n + 1)), UNSURE]}
                    for name, n in groups.items()
                },
            },
        },
        "required": ["fits", "reason", "picks"],
    }
