import asyncio

import pytest

from app.services import slots as sl


def _slots(question: str, **cond) -> dict:
    s = sl.empty_slots(question)
    s["conditions"].update(cond)
    return sl.correct(question, s)


def test_normalize_fills_missing_keys_and_drops_bad_values() -> None:
    s = sl.normalize(
        "질문",
        {
            "topic": " 혼인 건수 ",
            "target": "",
            "conditions": {"classes": "연령", "sex": "남녀", "region_level": "구", "year": "2024"},
            "query_type": "모름",
        },
    )
    assert s["topic"] == "혼인 건수"
    assert s["target"] is None
    c = s["conditions"]
    assert (c["classes"], c["items"], c["sex"], c["region_level"], c["year"]) == (
        ["연령"],
        [],
        None,
        None,
        2024,
    )
    assert s["query_type"] == "find_table"


def test_year_only_from_question() -> None:
    assert _slots("외국인 주민 수", year=2024)["conditions"]["year"] is None
    c = _slots("2010년부터 2020년까지 출생아 수")["conditions"]
    assert (c["year"], c["year_end"]) == (2010, 2020)


def test_cohort_year_is_not_the_observed_period() -> None:
    """'입국연도가 2024년'은 분류 값이다.
    기준 연도가 따로 있으면 조회 기간(2024~2025)으로 읽지 않는다"""
    c = _slots("2025년 기준 서울 외국인 중 입국연도가 2024년인 사람은 몇 명이야?")["conditions"]
    assert (c["year"], c["year_end"]) == (2025, None)
    c = _slots("서울 외국인 중 입국연도가 2024년인 사람 수")["conditions"]
    assert (c["year"], c["year_end"]) == (2024, None)
    c = _slots(
        "2025년 기준 서울 외국인 중 2024년에 입국한 베트남 사람은 몇 명이야?",
        items=["2024년 입국", "베트남"],
    )["conditions"]
    assert (c["year"], c["year_end"]) == (2025, None)
    assert (c["classes"], c["items"]) == (["입국연도"], ["2024년", "베트남"])


def test_sex() -> None:
    assert _slots("미혼 여성 인구", sex=None)["conditions"]["sex"] == "여"
    assert _slots("혼인 건수", sex="전체")["conditions"]["sex"] is None
    both = _slots("남녀별 혼인 건수")["conditions"]
    assert (both["sex"], both["classes"]) == (None, ["성별"])
    assert _slots("남녀 합계 출생아 수")["conditions"]["sex"] == "전체"


def test_region_and_level() -> None:
    c = _slots("부산 외국인 주민 수", region="부산")["conditions"]
    assert (c["region"], c["region_level"]) == ("부산광역시", "시도")
    c = _slots("부산 구별 외국인 주민 수", region="부산")["conditions"]
    assert c["region_level"] == "시군구"
    c = _slots("부산광역시 해운대구 인구", region="부산광역시 해운대구")["conditions"]
    assert c["region_level"] == "시군구"
    assert _slots("세대구성별 가구별 인구")["conditions"]["region_level"] is None
    assert _slots("혼인 건수", region_level="시도")["conditions"]["region_level"] is None


def test_classes_and_items_cleanup() -> None:
    s = sl.empty_slots("2010년 시도별 출생아 수")
    s["topic"] = "출생아 수"
    s["conditions"].update(classes=["시도", "연령"], items=["출생아 수", "미혼"])
    c = sl.correct("2010년 시도별 출생아 수", s)["conditions"]
    assert (c["classes"], c["items"]) == (["연령"], ["미혼"])
    c = _slots("남녀별 1인 가구", items=["남", "여"])["conditions"]
    assert (c["classes"], c["items"]) == (["성별"], [])


def test_extract_retries_then_falls_back(monkeypatch) -> None:
    calls = []

    async def fake_chat(question: str, temperature: float) -> str:
        calls.append(temperature)
        return "JSON을 만들 수 없습니다"

    monkeypatch.setattr(sl, "_chat", fake_chat)
    trace: dict = {}
    s = asyncio.run(sl.extract("2024년 서울 시군구별 여성 인구", trace))
    assert calls == list(sl.TEMPERATURES)
    assert s["parse_error"] is True
    c = s["conditions"]
    assert (c["year"], c["sex"], c["region_level"]) == (2024, "여", "시군구")
    assert len(trace["hcx_raw"]) == 2


def test_extract_reads_json_in_code_block(monkeypatch) -> None:
    async def fake_chat(question: str, temperature: float) -> str:
        return '```json\n{"topic": "귀화자 수", "target": "혼인 귀화자", "conditions": {}}\n```'

    monkeypatch.setattr(sl, "_chat", fake_chat)
    s = asyncio.run(sl.extract("혼인 귀화자 수"))
    assert (s["topic"], s["target"], s["parse_error"]) == ("귀화자 수", "혼인 귀화자", False)


def test_target_cleanup() -> None:
    s = sl.empty_slots("서울 구별 65세 이상 인구")
    s["target"] = "65세 이상"
    s["conditions"]["classes"] = ["구별"]
    out = sl.correct("서울 구별 65세 이상 인구", s)
    assert (out["target"], out["conditions"]["items"], out["conditions"]["classes"]) == (
        None,
        ["65세 이상"],
        [],
    )
    s = sl.empty_slots("외국인 근로자 남자")
    s["target"] = "남자"
    assert sl.correct("외국인 근로자 남자", s)["target"] is None


def test_householder_sex_is_a_class() -> None:
    c = _slots("여성 가구주 가구 수", sex="여")["conditions"]
    assert (c["sex"], c["classes"]) == (None, ["가구주 성별"])
    s = sl.empty_slots("연령별 미혼 여성 인구")
    s["topic"] = "미혼 인구"
    s["conditions"]["items"] = ["미혼"]
    assert sl.correct("연령별 미혼 여성 인구", s)["conditions"]["items"] == ["미혼"]


def test_short_year_and_projection() -> None:
    assert _slots("95년 인구 통계")["conditions"]["year"] == 1995
    assert _slots("'05년도 출생아")["conditions"]["year"] == 2005
    for q in ("10년 전 대비 인구", "90년생 인구", "20년간 인구 변화"):
        assert _slots(q)["conditions"]["year"] is None
    assert _slots("2050년 인구")["projection"] is True
    assert _slots("앞으로 인구가 어떻게 될지")["projection"] is True
    assert _slots("2010년 인구")["projection"] is False


def test_region_words() -> None:
    assert _slots("우리 구 인구")["conditions"]["region_level"] == "시군구"
    assert _slots("우리 동네 외국인 주민 수")["conditions"]["region_level"] == "시군구"
    assert _slots("동 단위 인구")["conditions"]["region_level"] == "읍면동"


def test_negation_and_spouse() -> None:
    assert _slots("귀화한 사람 말고 결혼이민자 통계")["exclude"] == ["귀화한"]
    assert _slots("65세 이상 제외 인구")["exclude"] == []
    s = _slots("외국인 여자와 결혼한 한국 남자")
    assert (s["spouse"], s["conditions"]["sex"]) == ("외국인 아내", None)
    assert _slots("한국 여자가 외국 남자랑 결혼한 건수")["spouse"] == "외국인 남편"
    q = "외국인 여자와 결혼한 한국 남자"
    s = sl.empty_slots(q)
    s["target"] = "한국 남자"
    assert sl.correct(q, s)["target"] is None
    s = _slots("외국인 여자 수")
    assert (s["spouse"], s["conditions"]["sex"]) == (None, "여")


def test_our_district_particle_is_not_guro():
    import copy

    base = {
        "topic": "이사",
        "search_query": None,
        "target": None,
        "exclude": [],
        "llm_exclude": [],
        "query_type": "find_table",
        "conditions": {
            "classes": [],
            "items": [],
            "sex": None,
            "region": "구로",
            "region_level": None,
            "year": None,
            "year_end": None,
        },
    }
    out = sl.correct("우리 구로 이사 오는 사람들 나이", copy.deepcopy(base))
    assert out["conditions"]["region"] is None and out["conditions"]["region_level"] == "시군구"
    b2 = copy.deepcopy(base)
    b2["conditions"]["region"] = "구로구"
    assert sl.correct("구로구로 이사 오는 사람", b2)["conditions"]["region"] == "구로구"


OUR_DISTRICT_CASES = [
    ("우리 구로 이사 오는 사람들 나이", "구로", True),
    ("우리 구로 이사 오는 사람들 나이", "구로구", True),
    ("우리 구로 이사 오는 사람들 나이", "서울 구로구", True),
    ("우리 구로 이사 오는 사람들 나이", "서울특별시 구로구", True),
    ("우리 구로 이사 오는 사람들 나이", "구", True),
    ("우리구로 이사 오는 사람", "구로구", True),
    ("우리 군에서 태어난 아이", "군", True),
    ("구로구 우리 구로 이사 오는 사람", "구로구", False),
    ("우리 구에 사는 외국인", "중구", False),
    ("부산 해운대구 인구", "해운대구", False),
    ("서울 구로 이사 오는 사람", "구로구", False),
    ("구로로 이사 오는 사람", "구로", False),
]


@pytest.mark.parametrize(("question", "region", "misread"), OUR_DISTRICT_CASES)
def test_our_district_misread(question: str, region: str, misread: bool) -> None:
    assert sl.our_district_misread(question, region) is misread
    assert sl._misread_by_regex(question, region) is misread


def test_our_district_misread_clears_the_region_gemma_wrote() -> None:
    s = sl.empty_slots("우리 구로 이사 오는 사람들 나이")
    s["conditions"]["region"] = "서울 구로구"
    out = sl.correct("우리 구로 이사 오는 사람들 나이", s)
    assert out["conditions"]["region"] is None and out["conditions"]["region_level"] == "시군구"


def test_slot_prompt_follows_the_search_llm(monkeypatch) -> None:
    import asyncio

    from app.services import search_llm
    from app.services.hcx_client import ChatResult, HcxClient

    seen = []

    async def chat(self, model, messages, **options):
        seen.append(messages[0]["content"])
        return ChatResult(content="{}", finish_reason="stop")

    monkeypatch.setattr(HcxClient, "chat", chat)
    monkeypatch.setattr(search_llm.GemmaClient, "chat", chat)
    for name in ("hcx", "gemma"):
        monkeypatch.setattr(search_llm.get_settings(), "search_llm", name, raising=False)
        asyncio.run(sl._chat("인구"))
    assert seen == [sl.SLOT_SYSTEM, sl.SLOT_SYSTEM_GEMMA]


def test_student_words():
    from app.services import agegroup, llm_pick

    assert llm_pick.OFF_DOMAIN.search("대학생 수") is None
    assert llm_pick.OFF_DOMAIN.search("고등학생 수") is None
    assert llm_pick.OFF_DOMAIN.search("한 반에 학생 수")
    a = agegroup.resolve("초등학생인 다문화 가정 아이")
    assert (a.lo, a.hi) == (7, 12)


def test_single_age_becomes_an_item_condition() -> None:
    assert _slots("서른 살 여성의 출산율")["conditions"]["items"] == ["30세"]
    assert _slots("30세 여성의 출산율", items=["30대"])["conditions"]["items"] == ["30대"]
    assert _slots("65세 이상 인구")["conditions"]["items"] == []
    assert _slots("나이를 한 살 단위로")["conditions"]["items"] == []
    out = _slots("서른 살 여성의 출산율")
    assert any(f["key"] == "items" and "30세" in f["reason"] for f in out["fixes"])


@pytest.mark.parametrize("question", ["서울 2030세대 인구", "2030 세대 서울 인구는?"])
def test_generation_is_not_a_year(question: str) -> None:
    out = _slots(question, year=2030)
    assert out["conditions"]["year"] is None and out["projection"] is False
    assert (out["age"]["lo"], out["age"]["hi"]) == (20, 39)
    assert _slots("2030년 서울 인구는?", year=2030)["projection"] is True


@pytest.mark.parametrize(
    "question",
    [
        "인구 2000명 이하인 시군구",
        "2000가구 넘는 읍면동",
        "2000여 곳",
        "인구 2000만 명 넘는 나라",
        "인구 2000 명 이하인 지역",
        "2000 가구 넘는 곳",
        "2000 명이나 되는 곳",
        "2000 명에서 3000 명 사이인 지역",
        "2000 명으로 줄어든 지역",
        "2000~3000명인 지역",
        "2000에서 3000명 사이",
        "인구 2000 이하인 시군구",
        "2000명당 의사 수",
        "2000명대인 지역",
        "2000회 이상",
        "2000km",
        "2000여만 명",
        "인구 2,000명 이상인 지역",
    ],
)
def test_count_with_unit_is_not_a_year(question: str) -> None:
    assert _slots(question)["conditions"]["year"] is None


@pytest.mark.parametrize(
    "question",
    [
        "2020만 보여줘",
        "2020 원주 인구",
        "2020 가구 수",
        "2020 명절 이동",
        "2020 개인사업자",
        "2020 명인전 관람객",
        "2018평창 올림픽",
        "2020여성 고용률",
        "2020건강검진",
        "2020 이후 인구",
    ],
)
def test_bare_year_still_a_year(question: str) -> None:
    assert _slots(question)["conditions"]["year"] in (2018, 2020)


@pytest.mark.parametrize(
    ("question", "year", "year_end"),
    [
        ("2020년 기준 인구 2000명 이상", 2020, None),
        ("2000년 인구 2000명", 2000, None),
        ("2000~2020년 인구 변화", 2000, 2020),
    ],
)
def test_year_beside_count(question: str, year: int, year_end: int | None) -> None:
    c = _slots(question)["conditions"]
    assert (c["year"], c["year_end"]) == (year, year_end)


def test_relative_year_beside_count() -> None:
    out = _slots("작년 인구가 2000명 이하인 지역")
    assert out["time_ref"]["expr"] == "작년"
    assert out["conditions"]["year"] == out["time_ref"]["year"]


@pytest.mark.parametrize(
    ("question", "sex_class"),
    [
        ("작년에 한국 남자가 베트남 여자랑 결혼한 거 몇 건이야?", False),
        ("농사나 고기잡이 하는 남자가 외국인 여자랑 결혼한 거 몇 건이야?", False),
        ("남자랑 여자 처음 결혼하는 나이 몇 살 차이 나?", True),
        ("2010년에 결혼 안 한 사람 남자랑 여자 중 누가 얼마나 더 많았어?", True),
    ],
)
def test_spouse_pair_is_not_a_sex_condition(question: str, sex_class: bool) -> None:
    c = sl.correct(question, sl.empty_slots(question))["conditions"]
    assert c["sex"] is None and ("성별" in c["classes"]) is sex_class
