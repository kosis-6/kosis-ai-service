import pytest

from app.services import cross_indicator as x
from app.services import lookup, qa


@pytest.mark.parametrize(
    "question",
    [
        "여자 아기 100명 태어날 때 남자 아기는 몇 명 태어나?",
        "여아 100명 출생할 때 남아는 몇 명이야?",
        "태어난 아기 성비 알려줘",
    ],
)
def test_birth_sex_ratio_words(question: str) -> None:
    assert qa.BIRTH_SEX_RATIO.search(question)


@pytest.mark.parametrize(
    "question",
    ["남녀 성비 알려줘", "거제시는 여자 100명당 남자 몇 명이야?", "작년에 아기 몇 명 태어났어?"],
)
def test_population_sex_ratio_is_not_birth(question: str) -> None:
    assert not qa.BIRTH_SEX_RATIO.search(question)


@pytest.mark.parametrize(
    ("question", "hit"),
    [
        ("작년에 혼인 신고 안 한 부모한테서 태어난 아기 비율 얼마야?", True),
        ("혼외 출생아 몇 명이야?", True),
        ("결혼 안 한 부모에게서 태어난 아이 수", True),
        ("혼인 신고 안 한 커플 몇 쌍이야?", False),
        ("작년에 아기 몇 명 태어났어?", False),
    ],
)
def test_out_of_wedlock_birth_words(question: str, hit: bool) -> None:
    found = bool(qa.OUT_OF_WEDLOCK.search(question) and qa.BIRTH_WORDS.search(question))
    assert found is hit


def test_bare_foreigner_rule_steps_aside_after_residents_coverage(monkeypatch) -> None:
    monkeypatch.setattr(lookup, "_table", lambda tbl_id: {"end_year": 2024})
    assert qa._past_foreign_residents({"year": 2025})
    assert not qa._past_foreign_residents({"year": 2024})
    assert not qa._past_foreign_residents({"year": None})


@pytest.mark.parametrize(
    "question",
    [
        "화성시 사는 사람 등록된 외국인까지 다 합치면 몇 명이야?",
        "화성시 인구랑 등록된 외국인까지 합치면 몇 명이야?",
    ],
)
def test_people_plus_registered_foreigners_is_a_sum(question: str) -> None:
    c = x.detect(question)
    assert c is not None and (c.num, c.den, c.form) == ("총인구", "등록외국인 수", "sum")


def test_people_alone_is_not_a_sum() -> None:
    assert x.detect("화성시 사는 사람 몇 명이야?") is None
