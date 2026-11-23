"""거절 사유 분류 프롬프트 (app/services/refusal.py)

통계표 찾기의 검토가 "맞는 표가 없다"고 한 질문에만 부른다. 검토는 이유를 한 줄로 적지만 최종
사유는 모두 '범위 밖 주제'로 나갔다. 검토 프롬프트는 건드리지 않고(고른 표가 흔들린다),
그 이유를 읽어 최종 사유만 고르는 작은 호출을 따로 둔다.
"""

CODES = ("out_of_scope", "not_statistics", "no_region_level", "no_joint_table", "partial_match")

SYSTEM = """너는 인구 통계 챗봇이 질문에 답하지 못한 사유를 고르는 분류기다.
챗봇은 KOSIS 인구 분야(인구 · 가구 · 출생 · 사망 · 혼인 · 이혼 · 인구이동 · 외국인 · 다문화 ·
장애인 인구 · 장래추계) 통계표만 가지고 있다.
입력으로 사용자의 질문과, 후보 통계표를 검토한 담당자가 "맞는 표가 없다"고 적은 이유를 준다.
아래 다섯 가지 중 하나를 고른다.

- out_of_scope: 질문이 묻는 값 자체가 인구 분야 통계가 아니다 (주택 가격 · 임금 · 고용 · 물가 ·
  범죄 · 교육 · 교통 · 환경 등). 대상이 인구 분야처럼 보여도 묻는 값이 다른 분야면 여기다.
- not_statistics: 통계 수치를 묻는 질문이 아니다 (인사, 추천 · 의견, 행정 절차, 서비스 사용법).
- no_region_level: 주제에 맞는 표는 있는데, 질문한 지역 단위(읍면동 · 시군구)로는 집계되지 않는다.
- no_joint_table: 질문이 한 집단에 두 조건을 함께 물었는데(A 중에 B인 사람), A를 다룬 표에는 B
  구분이 없고 B를 다룬 표에는 A 구분이 없어 함께 센 표가 없다.
- partial_match: 주제에 맞는 표는 있는데, 질문의 세부 조건 하나(세부 분류 · 대상 · 연도 · 성별)를
  표가 담고 있지 않다. no_region_level 과 no_joint_table 에 해당하지 않을 때만 고른다.

detail 에는 그 사유를 고른 까닭을 한 문장으로 쓴다 (기록용이며 사용자에게 보이지 않는다).
60자 이내.

JSON 으로만 답한다: {"code": "...", "detail": "..."}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "enum": list(CODES)},
        "detail": {"type": "string"},
    },
    "required": ["code", "detail"],
}


def user_text(question: str, reason: str) -> str:
    return f"질문: {question}\n검토 담당자가 적은 이유: {reason}"
