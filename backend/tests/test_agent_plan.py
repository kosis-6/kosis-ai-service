"""계획의 코드 보정(app/agent/plan.py): LLM이 내놓은 계획을 질문 원문으로 다시 확인한다"""

from datetime import date

from app.agent import plan as pl

TODAY = date(2026, 11, 12)


def _plan(indicators: list[dict], **shared) -> dict:
    return {"shared": shared, "indicators": indicators, "combine": {"op": "none"}}


def _ind(span: str, search: str | None = None, scope: str = "in", **override) -> dict:
    return {"span": span, "search": search or span, "scope": scope, "override": override}


def test_parse_broken_output_is_an_empty_plan() -> None:
    for content in ("", "지표는 두 개입니다", "[1, 2]", '{"indicators": "없음", "shared": null}'):
        p = pl.parse(content)
        assert (p["shared"], p["indicators"], p["combine"]) == ({}, [], {"op": "none"})
    # 모양이 다른 지표는 버린다
    assert pl.parse('{"indicators": [{"span": "혼인"}, "이혼"]}')["indicators"] == [
        {"span": "혼인"}
    ]


def test_invented_conditions_are_dropped() -> None:
    p = _plan(
        [_ind("혼인 건수", age="30대"), _ind("이혼 건수", sex="여")],
        year_from=2020,
        year_to=2020,
        sex="남",
    )
    dropped = pl.ground_plan("시도별 혼인 건수와 이혼 건수", p)
    assert p["shared"] == {}
    assert [i["override"] for i in p["indicators"]] == [{}, {}]
    assert {k for k, _ in dropped} == {"year_from", "year_to", "sex", "age"}


def test_conditions_the_question_states_are_kept() -> None:
    p = _plan([_ind("사망자 수", age="80세 이상")], year_from=2015, year_to=2025, sex="남")
    assert pl.ground_plan("2015~2025년 남성 80세 이상 사망자 수", p) == []
    assert p["shared"] == {"year_from": 2015, "year_to": 2025, "sex": "남"}
    # 연령 집단 이름으로 물었으면 숫자로 푼 연령도 근거가 있다
    p = _plan([_ind("노인 인구", age="65세 이상")])
    assert pl.ground_plan("시도별 노인 인구", p) == []


def test_relative_time_is_resolved_by_rule_not_by_the_llm() -> None:
    p = _plan([_ind("출생아 수")], year_from=2024, year_to=2024)
    fixes = pl.fix_periods_and_level("작년 출생아 수와 사망자 수", p, TODAY)
    assert (p["shared"]["year_from"], p["shared"]["year_to"]) == (2025, 2025)
    assert fixes[0][:2] == ("상대시점", "작년")


def test_each_indicator_takes_the_year_written_before_it() -> None:
    p = _plan([_ind("출생아 수"), _ind("사망자 수")], year_from=2020, year_to=2025)
    pl.fix_periods_and_level("2020년 출생아 수와 2025년 사망자 수", p, TODAY)
    years = [(i["override"]["year_from"], i["override"]["year_to"]) for i in p["indicators"]]
    assert years == [(2020, 2020), (2025, 2025)]
    assert "year_from" not in p["shared"]
    # 기간으로 물었으면 나누지 않는다
    p = _plan([_ind("출생아 수"), _ind("사망자 수")], year_from=2020, year_to=2025)
    pl.fix_periods_and_level("2020년부터 2025년까지 출생아 수와 사망자 수", p, TODAY)
    assert (p["shared"]["year_from"], p["shared"]["year_to"]) == (2020, 2025)


def test_region_level_follows_the_question() -> None:
    p = _plan([_ind("혼인 건수")], region_level="시도")
    pl.fix_periods_and_level("시군구별 혼인 건수", p, TODAY)
    assert p["shared"]["region_level"] == "시군구"
    # 질문에 지역 단위가 없으면 LLM이 붙인 단위는 지운다
    p = _plan([_ind("혼인 건수")], region_level="시군구")
    pl.fix_periods_and_level("혼인 건수와 이혼 건수", p, TODAY)
    assert "region_level" not in p["shared"]


def test_indicator_the_plan_missed_is_added() -> None:
    p = _plan([_ind("1인 가구 비율", "1인 가구")])
    fixes = pl.fix_indicators("서울의 1인 가구 비율, 노령화지수, 장애인 수", p)
    assert [i["span"] for i in p["indicators"]] == ["1인 가구 비율", "노령화지수", "장애인"]
    assert ("빠진 지표 추가", "장애인") in fixes


def test_population_indicator_marked_out_of_scope_comes_back() -> None:
    p = _plan(
        [_ind("고용률", "시도별 고용률", scope="out"), _ind("집값", "주택 가격", scope="out")]
    )
    pl.fix_indicators("고용률이 높은데 집값이 싼 지역", p)
    assert [i["scope"] for i in p["indicators"]] == ["out", "out"]  # 인구 낱말이 없으면 그대로
    p = _plan([_ind("청년 인구", "시도별 청년 인구", scope="out")])
    pl.fix_indicators("청년 인구", p)
    assert p["indicators"][0]["scope"] == "in"


def test_derived_indicator_gets_base_search_words() -> None:
    p = _plan([_ind("성비", "시도별 성비")])
    pl.fix_indicators("시도별 성비", p)
    assert p["indicators"][0]["search"] == "시도별 성비 성별 인구"


def test_whole_indicator_does_not_inherit_sex_and_age() -> None:
    p = _plan(
        [_ind("30대 남성 1인 가구", "연령별 1인 가구"), _ind("전체 출생아 수", "출생아 수")],
        sex="남",
        age="30대",
    )
    pl.fix_indicators("30대 남성 1인 가구와 전체 출생아 수", p)
    whole = p["indicators"][1]
    cond = pl.merged_conditions(p["shared"], whole["override"], whole.get("no_inherit") or ())
    assert cond == {}


def test_region_question_without_level_defaults_to_sido() -> None:
    p = _plan([_ind("혼인 건수"), _ind("이혼 건수")])
    fixes = pl.fix_indicators("혼인 건수보다 이혼 건수가 많은 지역", p)
    assert p["shared"]["region_level"] == "시도" and fixes[-1][0] == "지역 기본 시도"


def test_merged_conditions_prefers_the_indicator() -> None:
    shared = {"region_level": "시도", "year_from": 2015, "year_to": 2025, "sex": "없음"}
    assert pl.merged_conditions(shared, {"year_from": 2020, "year_to": 2020, "age": "30대"}) == {
        "region_level": "시도",
        "year_from": 2020,
        "year_to": 2020,
        "age": "30대",
    }


def test_render_makes_a_one_table_question() -> None:
    cond = {"region_level": "시군구", "year_from": 2015, "year_to": 2025, "sex": "남"}
    cond["age"] = "80세 이상"
    assert (
        pl.render({"search": "사망자 수"}, cond) == "2015~2025년 시군구별 80세 이상 남자 사망자 수"
    )
    # 검색어에 이미 있는 말은 다시 붙이지 않는다
    assert pl.render({"search": "시도별 성별 인구"}, {"region_level": "시도", "sex": "남녀"}) == (
        "시도별 성별 인구"
    )


def test_correct_records_what_it_changed() -> None:
    p = _plan([_ind("혼인 건수", sex="여")], year_from=2019, year_to=2019)
    p = pl.correct("시도별 혼인 건수와 이혼 건수", p, TODAY)
    assert ("sex", "여") in p["_dropped"] and ("year_from", 2019) in p["_dropped"]
    assert ("빠진 지표 추가", "이혼 건수") in p["_fixed"]
    assert p["shared"] == {"region_level": "시도"}
