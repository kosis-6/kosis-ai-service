import pytest

from app.services import indicators

pytestmark = pytest.mark.needs_data


@pytest.mark.parametrize(
    ("question", "year", "name", "code_prefix"),
    [
        ("부천 원미구", 2025, "원미구", "41192"),
        ("부천시 원미구", 2025, "원미구", "41192"),
        ("경기 부천 원미구", 2025, "원미구", "41192"),
        ("부천 원미구", 2015, "원미구", "41195"),
        ("수원 영통구", 2025, "영통구", "41117"),
        ("성남 분당", 2025, "분당구", "41135"),
        ("고양 일산동구", 2025, "일산동구", "4128"),
        ("창원 의창구", 2025, "의창구", "48121"),
        ("포항 남구", 2025, "남구", "4711"),
    ],
)
def test_city_and_gu(question, year, name, code_prefix):
    r = indicators.resolve_region(question, year=year)
    assert r["status"] == "ok", r
    assert r["region_name"] == name
    assert r["region_code"].startswith(code_prefix)


@pytest.mark.parametrize(
    ("question", "year", "code"),
    [
        ("창원시", 2025, "48120"),
        ("창원", 2025, "48120"),
        ("창원시", 2005, "48110"),
        ("창원시", None, "48120"),
        ("원미구", None, "41192"),
    ],
)
def test_same_place_old_and_new_code(question, year, code):
    r = indicators.resolve_region(question, year=year)
    assert r["status"] == "ok", r
    assert r["region_code"] == code


def test_sido_and_gu_still_works():
    r = indicators.resolve_region("서울 중구", year=2025)
    assert r["status"] == "ok" and r["region_code"].startswith("11")


@pytest.mark.parametrize(
    ("regions", "question", "expected"),
    [
        (["부천", "원미구"], "2025년 부천 원미구 인구", ["부천 원미구"]),
        (["부천시", "원미구"], "부천시 원미구 인구", ["부천시 원미구"]),
        (["수원", "영통구"], "수원 영통구 인구", ["수원 영통구"]),
        (["창원", "의창구"], "창원 의창구 인구", ["창원 의창구"]),
        (["경기", "부천", "원미구"], "경기 부천 원미구 인구", ["경기 부천 원미구"]),
        (["부천", "원미구"], "부천과 원미구 인구 비교", ["부천", "원미구"]),
        (["서울", "중구"], "서울 중구 인구", ["서울 중구"]),
        (["서울", "부산"], "서울과 부산 인구 비교", ["서울", "부산"]),
    ],
)
def test_merge_city_and_gu(regions, question, expected):
    from app.services import qa

    assert qa._merge_regions(regions, question) == expected


@pytest.mark.parametrize(
    ("question", "regions", "expected"),
    [
        # 11/27 차트 점검: 추이는 지역을 하나씩 조회해 '광주'를 경기 광주시와 헷갈린다고 되물었다
        (
            "서울 부산 대구 인천 광주 대전 울산 출생아 수 추이",
            ["서울", "부산", "대구", "인천", "광주", "대전", "울산"],
            ["서울", "부산", "대구", "인천", "광주광역시", "대전", "울산"],
        ),
        # 혼자 말하거나 시군구와 함께 말하면 그대로 두고 되묻는다
        ("광주 인구 추이", ["광주"], ["광주"]),
        ("광주와 용인 인구", ["광주", "용인"], ["광주", "용인"]),
    ],
)
def test_ambiguous_name_among_sido_is_the_sido(question, regions, expected):
    from datetime import date

    from app.services import qa

    parsed = {"type": "trend", "indicator": "출생아 수", "regions": regions, "sex": "계"}
    assert qa.correct(question, parsed, date(2026, 11, 27))["regions"] == expected


SIDO_17 = [
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시",
    "울산광역시", "세종특별자치시", "경기도", "강원도", "충청북도", "충청남도", "전라북도",
    "전라남도", "경상북도", "경상남도", "제주특별자치도",
]  # fmt: skip


@pytest.mark.parametrize(
    ("question", "parsed", "level", "sido"),
    [
        # 11/27 차트 점검: HCX가 펼친 17개 시도를 '질문에 없는 지역'으로 막았다
        (
            "2024년 시도별 고령인구 비율 비교해줘",
            {"indicator": "고령인구 비율", "regions": SIDO_17, "level": "시도"},
            "시도",
            None,
        ),
        # 11/27 차트 점검: '시군별'을 못 읽고 경기도 값 하나로 답했다
        (
            "2024년 경기도 시군별 인구 비교",
            {"indicator": "총인구", "regions": ["경기도"], "sido": "경기도", "level": "시군구"},
            "시군구",
            "경기도",
        ),
    ],
)
def test_compare_by_region_without_named_regions_is_a_list(question, parsed, level, sido):
    from datetime import date

    from app.services import qa

    base = {"type": "compare", "year": 2024, "sex": "계"}
    p = qa.correct(question, {**base, **parsed}, date(2026, 11, 27))
    assert p["type"] == "rank" and p["level"] == level and p.get("sido") == sido
    assert not p.get("regions") and not p.get("ungrounded_region")


@pytest.mark.parametrize(
    "question", ["2024년 서울과 부산 인구 비교", "서울과 부산의 구별 인구 비교"]
)
def test_compare_of_named_regions_stays_a_compare(question):
    """지역을 둘 이상 말한 비교는 '별'이 있어도 그 지역끼리의 비교로 둔다"""
    from datetime import date

    from app.services import qa

    parsed = {"type": "compare", "indicator": "총인구", "regions": ["서울", "부산"], "year": 2024}
    p = qa.correct(question, {**parsed, "sex": "계"}, date(2026, 11, 27))
    assert p["type"] == "compare" and p["regions"] == ["서울", "부산"]


def test_districts_come_from_region_codes():
    """일반구는 시 이름 목록이 아니라 행정구역 코드(시 코드 앞 4자리 + kind='일반구')로 찾는다"""
    names = [r["region_name"] for r in indicators.districts("수원시")]
    assert names == ["장안구", "권선구", "팔달구", "영통구"]
    assert len(indicators.districts("성남시")) == 3
    assert indicators.districts("서울") == [] and indicators.districts("강남구") == []


@pytest.mark.parametrize(
    ("question", "kind", "city", "n"),
    [
        # PR 리뷰: 시도만 범위로 남아 전국 시군구 순위로 넓어졌다
        ("2024년 수원시 구별 인구 비교", "compare", "수원시", 4),
        ("수원시 구별 인구", "value", "수원시", 4),
        ("2024년 성남시 구별 인구 비교", "compare", "성남시", 3),
    ],
)
def test_city_by_district_lists_its_districts(question, kind, city, n):
    from datetime import date

    from app.services import qa

    parsed = {"type": kind, "indicator": "총인구", "regions": [city], "year": 2024, "sex": "계"}
    p = qa.correct(question, parsed, date(2026, 11, 27))
    assert p["type"] == "rank" and p["within"] == f"경기도 {city}" and not p.get("regions")
    ranking = qa.compute(p)["ranking"]
    assert len(ranking) == n and all(r["region"].startswith(f"경기도 {city}") for r in ranking)


def test_by_district_without_districts_keeps_the_sido_scope():
    """'시군구별'의 '구별'이나 일반구가 없는 시도는 시 안의 구 범위로 읽지 않는다"""
    from datetime import date

    from app.services import qa

    parsed = {"type": "compare", "indicator": "총인구", "regions": ["경기도"], "sido": "경기도"}
    p = qa.correct("2024년 경기도 시군구별 인구 비교", {**parsed, "year": 2024, "sex": "계"},
                   date(2026, 11, 27))  # fmt: skip
    assert p.get("within") is None and p["sido"] == "경기도"


@pytest.mark.parametrize(
    ("question", "parsed", "within", "n"),
    [
        # PR 리뷰: 한 해 순위로 바뀌어 두 연도를 안 썼고, 되돌리면 경기도 전체 추이 하나였다
        (
            "2020년과 2024년 경기도 시군별 인구 비교",
            {"type": "trend", "regions": ["경기도"], "level": "시군구"},
            None,
            31,
        ),
        (
            "2020년과 2024년 수원시 구별 인구 비교",
            {"type": "compare", "regions": ["수원시"]},
            "경기도 수원시",
            4,
        ),
    ],
)
def test_two_years_by_region_lists_both_years(question, parsed, within, n):
    from datetime import date

    from app.services import qa

    base = {"indicator": "총인구", "start_year": 2020, "end_year": 2024, "sex": "계"}
    p = qa.correct(question, {**base, **parsed}, date(2026, 11, 27))
    assert p["type"] == "rank" and p["change_from"] == 2020 and p["year"] == 2024
    assert p.get("within") == within
    r = qa.compute(p)
    assert len(r["ranking"]) == n == r["total_regions"]
    first = r["ranking"][0]
    assert first["value"] == first["to"] - first["from"]
    assert qa.summary(r).startswith("2020년 → 2024년 총인구가 가장 많이 늘어난")
    assert "2020년 " in qa.render(r).split("\n")[1] and "→ 2024년 " in qa.render(r)


@pytest.mark.parametrize(
    "question",
    ["2020년부터 2024년까지 경기도 시군별 인구 추이", "2020년과 2024년 서울 인구 비교"],
)
def test_whole_period_or_one_region_stays_a_trend(question):
    """기간 전체('추이')나 지역 하나는 지역별 두 해 목록으로 바꾸지 않는다"""
    from datetime import date

    from app.services import qa

    regions = ["서울"] if "서울" in question else ["경기도"]
    parsed = {"type": "trend", "indicator": "총인구", "regions": regions,
              "start_year": 2020, "end_year": 2024, "sex": "계"}  # fmt: skip
    p = qa.correct(question, parsed, date(2026, 11, 27))
    assert p["type"] == "trend" and not p.get("rank_by")


def test_two_year_change_of_a_ratio_is_rounded_in_percent_points():
    """11/27 서버 확인: '2015년과 2024년 시도별 고령인구 비율 비교'가 9.249999999999998%로 나왔다"""
    from datetime import date

    from app.services import qa

    parsed = {"type": "compare", "indicator": "고령인구 비율", "level": "시도", "sex": "계",
              "start_year": 2015, "end_year": 2024, "ages": [[65, None]]}  # fmt: skip
    question = "2015년과 2024년 시도별 고령인구 비율 비교"
    r = qa.compute(qa.correct(question, parsed, date(2026, 11, 27)))
    assert r["unit"] == "%p" and r["value_unit"] == "%"
    for x in r["ranking"]:
        assert x["value"] == round(x["to"] - x["from"], 2)
    assert "%p" in qa.summary(r) and "e-" not in qa.summary(r)


def test_difference_keeps_the_inputs_decimals():
    from app.services import qa

    assert qa._difference(23.9, 14.65) == 9.25
    assert qa._difference(968821, 855248) == 113573


def test_two_indicators_by_region_lists_each_indicator():
    """PR 리뷰: 목록으로 먼저 바뀌어 사망자 수가 빠졌다 → 지표마다 목록을 계산해 합친다"""
    from datetime import date

    from app.services import qa

    parsed = {"type": "compare", "indicator": "출생아 수", "regions": ["경기도"], "sido": "경기도",
              "level": "시군구", "year": 2024, "sex": "계"}  # fmt: skip
    p = qa.correct("2024년 경기도 시군별 출생아 수와 사망자 수 비교", parsed, date(2026, 11, 27))
    assert p["type"] == "rank" and p["indicators"] == ["출생아 수", "사망자 수"]
    r = qa.compute(p)
    assert [(x["indicator"], len(x["ranking"])) for x in r["split"]] == [
        ("출생아 수", 31),
        ("사망자 수", 31),
    ]


def test_sigungu_deaths_add_up_to_the_sido_value():
    """사망자 수의 시군구 출처(DT_1B8000I T20)는 시도 표(DT_1B8000H T20)의 값과 합이 같다"""
    from app.services import qa

    base = {"indicator": "사망자 수", "year": 2024, "sex": "계"}
    towns = qa.compute({**base, "type": "rank", "level": "시군구", "sido": "경기도"})["ranking"]
    sido = qa.compute({**base, "type": "value", "regions": ["경기도"]})["value"]
    assert len(towns) == 31 and sum(x["value"] for x in towns) == sido


@pytest.mark.parametrize(
    ("question", "conditions", "changes"),
    [
        # 11/27 PR #261 점검: 목록 뒤 지역만 말하면 새 질문으로 읽어 답하지 못했다
        ("부산은?", {"type": "rank", "sido": "경기도", "level": "시군구"},
         {"sido": "부산", "level": "시군구"}),
        ("성남시는?", {"type": "rank", "within": "경기도 수원시", "level": "시군구"},
         {"within": "경기도 성남시", "sido": None}),
        ("부산은?", {"type": "rank", "within": "경기도 수원시", "level": "시군구"},
         {"sido": "부산", "level": "시군구", "within": None}),
        # 원래 말투('경기도 안에서는?')는 범위 없는 시도 순위 뒤에도 그대로
        ("경기도 안에서는?", {"type": "rank", "level": "시도"},
         {"sido": "경기도", "level": "시군구"}),
        # 범위 없는 순위 뒤 지역만 · 시도 범위 뒤 구 있는 시 · 목록이 아닌 답은 이 규칙이 아니다
        ("부산은?", {"type": "rank", "level": "시도"}, None),
        ("성남시는?", {"type": "rank", "sido": "경기도", "level": "시군구"}, None),
        ("부산은?", {"type": "value", "regions": ["서울"]}, None),
    ],
)  # fmt: skip
def test_region_after_a_scoped_list_moves_the_scope(question, conditions, changes):
    from app.services import ask_session

    assert ask_session.rank_scope_changes(question, conditions) == changes


def test_within_change_is_applied():
    from app.services import ask_session

    before = {"type": "rank", "indicator": "총인구", "within": "경기도 수원시", "year": 2024}
    after, changed = ask_session.apply_changes(before, {"within": "경기도 성남시", "sido": None})
    assert after["within"] == "경기도 성남시" and after["sido"] is None and "view" in changed


def test_change_rank_uses_the_asked_start_year():
    """11/27 PR #261 점검: '2015년 대비 2024년 … 가장 많이 줄어든'을 2023 → 2024년으로 계산했다"""
    from app.services import qa

    q = {"type": "rank", "indicator": "총인구", "level": "시도", "sex": "계", "year": 2024,
         "start_year": 2015, "end_year": 2024, "rank_by": "decrease", "top": 3}  # fmt: skip
    r = qa.compute(q)
    assert r["change_from"] == 2015 and r["notes"][0].startswith("2015년 → 2024년")
    assert qa.compute({**q, "start_year": None, "end_year": None})["change_from"] == 2023
