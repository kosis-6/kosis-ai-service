"""모호한 질문 감지와 답변 병합. 숫자를 계산하거나 AI·DB를 호출하지 않는다."""

import pytest

from app.services import question_clarify as c


@pytest.mark.parametrize(
    "question", ["우리 동네 인구는?", "우리구는 몇 명이야?", "내가 사는 곳 출생아 수"]
)
def test_local_region_requires_name(question):
    assert c.detect(question, [])["kind"] == "local_region"


def test_explicit_region_or_nation_does_not_ask_local_region():
    assert c.detect("우리 동네 부산 출생아 수", ["부산"]) is None
    assert c.detect("우리나라 인구는?", []) is None
    assert c.detect("우리 시민의 수는?", []) is None


@pytest.mark.parametrize(
    "question",
    [
        "2024년 수원에서 이사한 건수는?",
        "서울에서 이사 간 사람은 몇 명이야?",
    ],
)
def test_outgoing_moves_ask_scope(question):
    assert c.detect(question, ["수원"])["options"] == c.MOVE_OPTIONS


@pytest.mark.parametrize(
    "question",
    [
        "수원으로 이사 온 건수는?",
        "서울에서 수원으로 이사한 건수는?",
        "수원에서 이사 온 사람은?",
        "수원에서 다른 지역으로 이사한 건수는?",
        "수원에서 이사한 이유는?",
        "수원에서 이사한 건수에 지역 내 이동도 포함해줘",
        "수원의 순이동은?",
        "수원 전출 건수는?",
    ],
)
def test_explicit_or_different_movement_question_is_not_overridden(question):
    assert c.detect(question, ["수원"]) is None


@pytest.mark.parametrize(
    "reply,scope",
    [
        ("1번이요", "include"),
        ("포함해줘", "include"),
        (c.MOVE_OPTIONS[0], "include"),
        ("2번", "exclude"),
        ("제외이요", "exclude"),
        (c.MOVE_OPTIONS[1], "exclude"),
        ("포함하지 마", None),
        ("1번 말고 2번", None),
        ("2024년 서울 출생아 수", None),
    ],
)
def test_scope_reply_does_not_guess(reply, scope):
    assert c.scope_reply(reply) == scope


def test_region_reply_and_rewrite_keep_original_conditions():
    assert c.region_reply("서울 중구요", ["서울", "중구"], False) == "서울 중구"
    assert c.region_reply("경기도 수원시요", ["경기도수원시"], False) == "경기도수원시"
    assert c.region_reply("서울 출생아 수", ["서울"], True) is None
    assert c.region_reply("서울 말고 부산", ["서울", "부산"], False) is None
    assert c.region_reply("모르겠어", [], False) is None
    assert c.merge_region("2024년 우리 동네 여성 인구는?", "수원시") == "2024년 수원시 여성 인구는?"


def test_selected_scope_is_preserved_in_search_question():
    original = "2024년 수원에서 이사한 건수"
    assert c.merge_scope(original, "include").startswith(original)
    assert "지역 내 이동과 지역 밖" in c.merge_scope(original, "include")
    assert "지역 내 이동 제외" in c.merge_scope(original, "exclude")
