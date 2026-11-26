import asyncio
from datetime import date

import pytest

from app.services import ask_session, indicators, lookup, qa
from app.services import cross_indicator as x


@pytest.fixture
def seoul_region(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve_region(name, year=None, sigungu=False):
        if name == "서울":
            return {"status": "ok", "display": name, "kind": "시도", "region_code": "11"}
        return {"status": "unknown_region", "region": name}

    monkeypatch.setattr(indicators, "_region_names", lambda: ("서울",))
    monkeypatch.setattr(indicators, "resolve_region", resolve_region)


@pytest.mark.parametrize(
    ("question", "indicator", "base"),
    [
        ("여자 아기 100명 태어날 때 남자 아기는 몇 명 태어나?", "출생성비", 100),
        ("여아 50명 출생할 때 남아는 몇 명이야?", "출생성비", 50),
        ("태어난 아기 성비 알려줘", "출생성비", None),
        ("남자 아기 200명 태어날 때 여자 아기는 몇 명?", "남아 100명당 여아 수", 200),
        ("여자아이는 남자아이 1,000명당 몇 명이야?", "남아 100명당 여아 수", 1000),
        ("남아 백 명당 여아는 몇 명이야?", "남아 100명당 여아 수", 100),
        ("남아 한 명을 기준으로 여아는 몇 명이야?", "남아 100명당 여아 수", 1),
    ],
)
def test_birth_sex_ratio_words(
    seoul_region, question: str, indicator: str, base: int | None
) -> None:
    parsed = qa.correct(
        f"2025년 서울 {question}",
        {"type": "value", "indicator": "출생아 수", "regions": ["서울"], "sex": "남"},
    )
    assert parsed["indicator"] == indicator
    assert parsed.get("birth_ratio_base") == base
    assert parsed["grounded"] is True
    assert parsed["sex"] == "계" and not parsed.get("split_sex")


@pytest.mark.parametrize(
    ("question", "indicator", "base"),
    [
        ("남아 이백 명당 여아는 몇 명이야?", "남아 100명당 여아 수", 200),
        ("남아가 이천 명 태어나면 여아는 몇 명?", "남아 100명당 여아 수", 2000),
        ("남아이십명당 여아는 몇 명이야?", "남아 100명당 여아 수", 20),
        ("여아 이백오십 명당 남아는 몇 명이야?", "출생성비", 250),
        ("남아 천오백 명당 여아는 몇 명이야?", "남아 100명당 여아 수", 1500),
        ("여아 이만 명당 남아는 몇 명이야?", "출생성비", 20000),
        ("남아 스물다섯 명당 여아는 몇 명이야?", "남아 100명당 여아 수", 25),
        ("여아 열한 명당 남아는 몇 명이야?", "출생성비", 11),
        ("남아 1만 명당 여아는 몇 명이야?", "남아 100명당 여아 수", 10000),
        ("여자아이가 2천 명 태어날 때 남자아이는?", "출생성비", 2000),
    ],
)
def test_birth_sex_ratio_reads_korean_numbers(question: str, indicator: str, base: int) -> None:
    assert qa.birth_sex_ratio_reference(question) == (indicator, base)


@pytest.mark.parametrize(
    ("reply", "base"),
    [("그럼 이백 명 기준으로는?", 200), ("스물다섯 명당은?", 25), ("1만 명 기준으로는?", 10000)],
)
def test_birth_ratio_count_reply_reads_korean_numbers(reply: str, base: int) -> None:
    count = ask_session.BIRTH_RATIO_COUNT_REPLY.fullmatch(reply)
    assert count and qa.birth_ratio_count(count["count"]) == base


@pytest.mark.parametrize(
    "question",
    [
        "남녀 성비 알려줘",
        "거제시는 여자 100명당 남자 몇 명이야?",
        "작년에 아기 몇 명 태어났어?",
        "서울에서 남자 아기 100명이 태어났어?",
        "남아 100명당 미숙아는 몇 명이야?",
    ],
)
def test_population_sex_ratio_is_not_birth(question: str) -> None:
    assert qa.birth_sex_ratio_reference(question) is None
    assert not qa.BIRTH_SEX_RATIO_WORDS.search(question)


@pytest.fixture
def birth_counts(monkeypatch: pytest.MonkeyPatch, seoul_region) -> None:
    counts = {"T2": 23453, "T3": 22063}

    def prepare(spec, names, y0, y1, sex, ages):
        regions = [{"display": names[0], "region_code": "11"}]
        return regions, spec["sources"][0], [], "", []

    def sum_counts(src, codes, y0, y1, item_field, filters, age_codes):
        return {(y0, codes[0]): counts[src[item_field]]}

    monkeypatch.setattr(indicators, "_data_years", lambda: (1993, 2025))
    monkeypatch.setattr(indicators, "kosis_url", lambda table: f"https://kosis.kr/{table}")
    monkeypatch.setattr(lookup, "refresh_if_reloaded", lambda: None)
    monkeypatch.setattr(lookup, "_prepare", prepare)
    monkeypatch.setattr(lookup, "_sum", sum_counts)
    monkeypatch.setattr(
        lookup,
        "_table",
        lambda table: {
            "tbl_nm": "시군구/성/출산순위별 출생",
            "org_nm": "국가데이터처",
            "basis": "",
        },
    )


@pytest.mark.parametrize(
    ("question", "expected", "calculation"),
    [
        (
            "여아 100명당 남아는 몇 명?",
            106.30,
            "남아 출생아 수 23,453명 ÷ 여아 출생아 수 22,063명 × 100",
        ),
        (
            "남아 100명당 여아는 몇 명?",
            94.07,
            "여아 출생아 수 22,063명 ÷ 남아 출생아 수 23,453명 × 100",
        ),
        (
            "여아 50명당 남아는 몇 명?",
            53.15,
            "남아 출생아 수 23,453명 ÷ 여아 출생아 수 22,063명 × 50",
        ),
        (
            "남아 1,000명당 여아는 몇 명?",
            940.73,
            "여아 출생아 수 22,063명 ÷ 남아 출생아 수 23,453명 × 1,000",
        ),
    ],
)
def test_birth_sex_ratio_calculation(birth_counts, question, expected, calculation) -> None:
    parsed = qa.correct(
        f"2025년 서울 {question}",
        {"type": "value", "indicator": "출생성비", "regions": ["서울"], "year": 2025, "sex": "계"},
    )
    result = qa.compute(parsed)

    assert result["status"] == "ok" and result["value"] == expected
    assert str(expected) in qa.summary(result)
    assert calculation in qa.render(result)
    if (base := parsed["birth_ratio_base"]) != 100:
        assert f"{base:,}명당" in qa.summary(result)


def test_birth_sex_ratio_reverse_followup(birth_counts, monkeypatch: pytest.MonkeyPatch) -> None:
    async def unexpected_extract(*args, **kwargs):
        pytest.fail("출생성비를 뒤집는 후속 질문에서 LLM을 호출했습니다")

    monkeypatch.setattr(ask_session, "extract", unexpected_extract)
    state = {
        "conditions": {
            "type": "value",
            "indicator": "출생성비",
            "regions": ["서울"],
            "year": 2025,
            "sex": "계",
            "birth_ratio_base": 50,
        },
        "sources": {"indicator": "user", "regions": "user", "period": "user"},
        "last": {"status": "ok", "answer": "여아 50명당 53.15명입니다."},
    }
    response, sources, _ = asyncio.run(ask_session._turn(state, "그럼 반대로는?"))

    assert response["request_type"] == ask_session.CHANGE
    assert response["parsed"]["indicator"] == "남아 100명당 여아 수"
    assert response["result"]["year"] == 2025 and response["result"]["region"] == "서울"
    assert response["result"]["value"] == 47.04
    assert "남아 50명당 여아 수" in response["answer"]

    state = ask_session.next_state(state, "그럼 반대로는?", response, sources)
    changed, sources, _ = asyncio.run(ask_session._turn(state, "그럼 1,000명 기준으로는?"))
    assert changed["result"]["value"] == 940.73
    assert {"name": "지표", "value": "남아 1,000명당 여아 수", "source": "user"} in (
        ask_session.applied(changed["parsed"], sources)
    )


@pytest.mark.parametrize("base", [0, -50])
def test_birth_sex_ratio_rejects_nonpositive_base(birth_counts, base: int) -> None:
    parsed = qa.correct(
        f"2025년 서울 남아 {base}명당 여아는 몇 명?",
        {"type": "value", "indicator": "출생성비", "regions": ["서울"], "year": 2025},
    )
    result = qa.compute(parsed)
    assert result["status"] == "band_mismatch"
    assert result["reason"] == "기준 인원은 1명 이상이어야 합니다."


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


@pytest.mark.parametrize(
    ("question", "past"),
    [
        ("외국인 작년에 얼마나 늘었어?", True),
        ("2025년 외국인 몇 명이야?", True),
        ("2024년 외국인 몇 명이야?", False),
        ("외국인은 몇 명인가요?", False),
        ("외국인 제일 많이 사는 데 세 곳 어디야?", False),
        ("재작년 외국인 몇 명이야?", False),
    ],
)
def test_bare_foreigner_rule_uses_years_said_in_question(monkeypatch, question, past) -> None:
    monkeypatch.setattr(lookup, "_table", lambda tbl_id: {"end_year": 2024})
    assert qa._past_foreign_residents(question, date(2026, 11, 25)) is past


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
