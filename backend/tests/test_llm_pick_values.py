from app.services import llm_pick


def test_class_values_text_shows_child_values() -> None:
    # 장애유형처럼 대표 값이 '합계'뿐인 분류도 AI 후보 설명에는 하위 값이 보여야 한다
    c = {"name": "장애유형별", "sample_values": ["합계"], "values": ["합계", "지체", "시각", "청각"]}
    assert llm_pick.class_values_text(c) == "합계, 지체, 시각, 청각"


def test_class_values_text_cuts_long_list() -> None:
    c = {"name": "성씨별", "sample_values": ["김"], "values": [f"성{i}" for i in range(20)]}
    assert llm_pick.class_values_text(c).endswith("성11 외 8개")


def test_class_values_text_falls_back_to_sample_values() -> None:
    assert llm_pick.class_values_text({"name": "x", "sample_values": ["a", "b"]}) == "a, b"
