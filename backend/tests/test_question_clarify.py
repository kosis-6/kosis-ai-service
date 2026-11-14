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
        "수원에서 이사 나간 사람 몇 명이야?",
        "전국에서 이사한 사람 수",
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
        ("포함하지 마", "exclude"),
        ("1번 말고 2번", "exclude"),
        ("2024년 서울 출생아 수", None),
        # 리뷰에서 받은 자유로운 답 (핵심 낱말·번호·서수로 읽는다)
        ("밖으로 나간 사람만", "exclude"),
        ("나간 것만", "exclude"),
        ("밖으로만", "exclude"),
        ("아니 밖으로만", "exclude"),
        ("빼고", "exclude"),
        ("두 번째", "exclude"),
        ("안에서 옮긴 것도 포함", "include"),
        ("다 포함", "include"),
        ("응 포함해줘", "include"),
        ("전부", "include"),
        ("모두요", "include"),
        ("첫 번째", "include"),
        ("둘 다요", "include"),
        # 뜻을 알 수 없거나 서로 어긋나면 짐작하지 않는다
        ("응", None),
        ("모르겠어", None),
        ("수원", None),
        ("밖으로 나간 것도 포함", None),
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
    assert "나간 전출 이동자수" in c.merge_scope(original, "exclude")


@pytest.mark.parametrize(
    "reply,name",
    [("수원", "수원"), ("수원이요", "수원"), ("강남요", "강남"), ("해운대", "해운대"), ("", None)],
)
def test_bare_region_keeps_only_the_name(reply, name):
    assert c.bare_region(reply) == name


@pytest.mark.parametrize(
    "question,scope",
    [
        ("수원에서 이사 나간 사람 몇 명이야?", "exclude"),
        ("부산에서 이사 떠난 사람 수", "exclude"),
        ("전국에서 이사한 사람 수", "nation"),
        ("수원에서 이사한 사람 몇 명이야?", None),  # 범위를 안 말함 → 되묻는다
        ("수원에서 이사한 이유는?", None),  # 다른 지표
    ],
)
def test_stated_scope_skips_asking(question, scope):
    assert c.stated_scope(question) == scope
    assert c.merge_scope(question, "nation") == question
