import pytest

from app.services import cross_indicator as x
from app.services import qa


@pytest.mark.parametrize(
    ("question", "num", "den", "scale", "form"),
    [
        ("2024년 서울 세대당 출생아 수는?", "출생아 수", "세대수", 1, "per"),
        ("서울 인구 1,000명당 외국인근로자 수", "외국인근로자 수", "총인구", 1000, "per"),
        ("인구 천명당 귀화자 수", "귀화자 수", "총인구", 1000, "per"),
        ("1000세대당 이혼 건수", "이혼 건수", "세대수", 1000, "per"),
        ("출생아 수 대비 사망자 수", "사망자 수", "출생아 수", 1, "versus"),
        ("이혼 건수 대비 혼인 건수 비율", "혼인 건수", "이혼 건수", 100, "versus"),
        ("외국인주민 수 중 귀화자 수 비율", "귀화자 수", "외국인주민 수", 100, "within"),
    ],
)
def test_detect(question: str, num: str, den: str, scale: int, form: str) -> None:
    c = x.detect(question)
    assert c is not None
    assert (c.num, c.den, c.scale, c.form) == (num, den, scale, form)
    assert c.official is None


@pytest.mark.parametrize(
    ("question", "official"),
    [
        ("인구 1000명당 혼인 건수", "조혼인율"),
        ("인구 1,000명당 출생아 수", "조출생률"),
        ("혼인 건수 중 다문화 혼인 건수 비율", "다문화 혼인 비중"),
        ("세대당 인구", "세대당 인구"),
    ],
)
def test_detect_official(question: str, official: str) -> None:
    c = x.detect(question)
    assert c is not None and c.official == official


@pytest.mark.parametrize(
    "question",
    [
        "서울 출생아 수와 사망자 수",
        "고령인구 비율 대비 출생아 수",
        "서울 출생아 수",
        "출생아 수 중 사망자 수",
    ],
)
def test_detect_none(question: str) -> None:
    assert x.detect(question) is None


def test_names_and_units() -> None:
    per = x.Cross("출생아 수", "세대수", 1)
    assert (per.name, per.unit, per.formula) == ("세대당 출생아 수", "명", "출생아 수 ÷ 세대수")
    per_n = x.Cross("귀화자 수", "총인구", 1000)
    assert per_n.name == "인구 1,000명당 귀화자 수" and per_n.unit == "명"
    assert per_n.formula == "귀화자 수 ÷ 총인구 × 1,000"
    vs = x.Cross("사망자 수", "출생아 수", 1, "versus")
    assert vs.name == "출생아 수 대비 사망자 수" and vs.unit == "배"
    rate = x.Cross("귀화자 수", "외국인주민 수", 100, "within")
    assert rate.name == "외국인주민 수 중 귀화자 수 비율" and rate.unit == "%"


def test_small_values_keep_three_significant_digits() -> None:
    assert x._div(41605, 4482063, 1) == 0.00928
    assert x._div(37734, 9331828, 1000) == 4.04
    assert x._div(1, 0, 1) is None


VALUES = {
    ("출생아 수", 2024): 41605,
    ("출생아 수", 2025): 45516,
    ("세대수", 2024): 4482063,
    ("세대수", 2025): 4501031,
    ("사망자 수", 2024): 46000,
}


def fake_run(latest: dict | None = None, sexless: tuple = ()):
    calls = []

    def run(q: dict) -> dict:
        name, kind = q["indicator"], q["type"]
        calls.append((name, q.get("year")))
        base = {
            "status": "ok",
            "indicator": name,
            "unit": "명",
            "source": f"{name} 표",
            "basis": f"{name} 기준",
            "tbl_id": f"T_{name}",
            "url": None,
            "notes": [],
            "sex": "계" if name in sexless else (q.get("sex") or "계"),
        }
        if kind == "value":
            year = q.get("year") or (latest or {}).get(name, 2024)
            if (name, year) not in VALUES:
                return {"status": "out_of_range", "indicator": name, "reason": "기간 밖"}
            return {**base, "region": "서울특별시", "year": year, "value": VALUES[(name, year)]}
        if kind == "trend":
            series = [{"year": y, "value": v} for (n, y), v in sorted(VALUES.items()) if n == name]
            return {**base, "region": "서울특별시", "series": series}
        rows = [{"region": "서울특별시", "value": 100}, {"region": "부산광역시", "value": 50}]
        if name == "세대수":
            rows = [{"region": "부산광역시", "value": 10}, {"region": "서울특별시", "value": 40}]
        key = "values" if kind == "compare" else "ranking"
        return {**base, "year": 2024, key: rows, "order": "높은 순", "level": "시도"}

    run.calls = calls
    return run


def q_of(kind: str, num="출생아 수", den="세대수", scale=1, **extra) -> dict:
    return {
        "type": kind,
        "regions": ["서울특별시"],
        "sex": "계",
        "cross": {"num": num, "den": den, "scale": scale, "form": "per"},
        **extra,
    }


def test_value_divides_and_shows_both_tables() -> None:
    res = x.compute(q_of("value", year=2024), fake_run())
    assert res["status"] == "ok" and res["method"] == "cross"
    assert res["value"] == 0.00928
    assert res["indicator"] == "세대당 출생아 수"
    assert [o["value"] for o in res["operands"]] == [41605, 4482063]
    assert res["tbl_id"] == "T_출생아 수"
    assert [e["tbl_id"] for e in res["extra_sources"]] == ["T_세대수"]


def test_value_without_year_uses_a_year_both_have() -> None:
    run = fake_run(latest={"출생아 수": 2024, "세대수": 2025})
    res = x.compute(q_of("value"), run)
    assert res["status"] == "ok" and res["year"] == 2024
    assert ("출생아 수", 2024) in run.calls and ("세대수", 2024) in run.calls


def test_value_without_year_keeps_values_of_the_same_month() -> None:
    inner = fake_run(latest={"출생아 수": 2025, "세대수": 2025})

    def run(q: dict) -> dict:
        out = inner(q)
        if out.get("status") == "ok" and not q.get("year"):
            out = {**out, "year": 2026, "as_of": "2026년 10월 말"}
        return out

    res = x.compute(q_of("value"), run)
    assert res["status"] == "ok" and res["year"] == 2026
    assert all(year is None for _, year in inner.calls)


def test_part_failure_is_passed_on_with_reason() -> None:
    res = x.compute(q_of("value", year=2030), fake_run())
    assert res["status"] == "out_of_range"
    assert "세대당 출생아 수를 계산하려면 출생아 수가 필요합니다" in res["reason"]


def test_sex_not_split_is_refused() -> None:
    res = x.compute(q_of("value", year=2024, sex="여"), fake_run(sexless=("세대수",)))
    assert res["status"] == "missing_data" and "value" not in res
    assert "성별로 나뉘어 있지 않습니다" in res["reason"]


def test_projection_is_refused() -> None:
    def run(q):
        res = fake_run()(q)
        return {**res, "basis": "장래인구추계"} if q["indicator"] == "세대수" else res

    res = x.compute(q_of("value", year=2024), run)
    assert res["status"] == "missing_data" and "추계" in res["reason"]


def test_trend_uses_years_in_both() -> None:
    res = x.compute(q_of("trend"), fake_run())
    assert [s["year"] for s in res["series"]] == [2024, 2025]
    assert res["change"]["from_year"] == 2024 and "rate" in res["change"]


def test_compare_matches_regions_and_sorts_high_first() -> None:
    res = x.compute(q_of("compare", year=2024, regions=["서울특별시", "부산광역시"]), fake_run())
    assert [v["region"] for v in res["values"]] == ["부산광역시", "서울특별시"]
    assert res["highest"] == "부산광역시" and res["gap"] == 2.5


def test_rank_reorders_by_divided_value() -> None:
    res = x.compute(q_of("rank", year=2024, top=1), fake_run())
    assert res["ranking"] == [{"region": "부산광역시", "value": 5.0, "rank": 1}]
    assert res["total_regions"] == 2


def test_ages_are_not_split() -> None:
    res = x.compute(q_of("value", year=2024, ages=[[20, 29]]), fake_run())
    assert res["status"] == "missing_data"


def test_correct_marks_cross_and_fixes_hcx_indicator() -> None:
    parsed = {"type": "value", "indicator": "출생아 수", "grounded": False}
    qa._cross("2024년 서울 세대당 출생아 수는?", parsed, lambda k, v, r: parsed.update({k: v}))
    assert parsed["cross"] == {"num": "출생아 수", "den": "세대수", "scale": 1, "form": "per"}
    assert parsed["indicator"] == "세대당 출생아 수" and parsed["grounded"] is True


def test_correct_sends_official_combination_to_dictionary() -> None:
    parsed = {"type": "other", "indicator": "혼인 건수"}
    qa._cross("2024년 부산 인구 1000명당 혼인 건수", parsed, lambda k, v, r: parsed.update({k: v}))
    assert parsed["indicator"] == "조혼인율" and "cross" not in parsed
    assert parsed["type"] == "value"


def test_correct_versus_read_as_compare_becomes_value() -> None:
    parsed = {"type": "compare", "indicator": "자연증가 건수", "regions": ["서울"]}
    qa._cross(
        "2024년 서울 출생아 수 대비 사망자 수는?", parsed, lambda k, v, r: parsed.update({k: v})
    )
    assert parsed["type"] == "value" and parsed["cross"]["form"] == "versus"


def test_same_table_is_listed_once() -> None:
    def run(q):
        return {**fake_run()(q), "tbl_id": "T_SAME", "source": "같은 표"}

    res = x.compute(q_of("value", year=2024), run)
    assert res["extra_sources"] == []


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import indicators, lookup

    regions = {"서울", "서울특별시", "부산"}
    monkeypatch.setattr(indicators, "find_regions", lambda t: [t] if t in regions else [])
    monkeypatch.setattr(indicators, "_data_years", lambda: (1993, 2025))
    monkeypatch.setattr(lookup, "refresh_if_reloaded", lambda: None)


@pytest.mark.parametrize(
    ("question", "num", "den", "scale", "dynamic"),
    [
        ("2020년 서울 인구 1,000명당 1인 가구 수는?", "1인 가구 수", "총인구", 1000, "num"),
        ("2020년 서울 세대당 1인 가구 수", "1인 가구 수", "세대수", 1, "num"),
        ("2020년 서울 인구 천 명당 통근 인구는 몇 명이야?", "통근 인구", "총인구", 1000, "num"),
        ("2020년 서울 1인 가구 대비 출생아 수", "출생아 수", "1인 가구", 1, "den"),
    ],
)
def test_detect_partial(no_db, question, num, den, scale, dynamic) -> None:
    c = x.detect_partial(question)
    assert c is not None
    assert (c.num, c.den, c.scale, c.dynamic) == (num, den, scale, dynamic)


@pytest.mark.parametrize(
    "question",
    [
        "2024년 서울 세대당 출생아 수",
        "2020년 서울 1인 가구 중 여성 비율",
        "2020년 서울 1인 가구 수",
    ],
)
def test_detect_partial_none(no_db, question) -> None:
    assert x.detect_partial(question) is None


def test_plain_words_keep_dictionary_indicator() -> None:
    assert x._side("여자 인구") == ("총인구", None)
    assert x._side("통근 인구") == (None, "통근 인구")
    assert x._side("1인 가구 수는") == (None, None)
    assert x._right_phrase(["1인", "가구", "수는"]) == "1인 가구 수"


def test_official_unmarried_ratio(no_db) -> None:
    assert x.detect("2020년 서울 인구 중 미혼 인구 비율은?").official == "미혼 비율"


def test_combine_value_uses_unit_of_dynamic_side() -> None:
    c = x.Cross("1인 가구 수", "총인구", 1000, dynamic="num", units={"1인 가구 수": "가구"})
    num = {"status": "ok", "indicator": "1인 가구 수", "region": "서울", "year": 2020,
           "sex": "계", "value": 1390701, "source": "1인가구 표", "basis": "인구총조사",
           "tbl_id": "T1"}  # fmt: skip
    den = {"status": "ok", "indicator": "총인구", "region": "서울", "year": 2020, "sex": "계",
           "value": 9668465, "source": "주민등록", "basis": "주민등록", "tbl_id": "T2"}  # fmt: skip
    res = x.combine_value(c, num, den, {"sex": "계"})
    assert res["value"] == 143.84 and res["unit"] == "가구"
    assert res["operands"][0]["unit"] == "가구"
    other_year = x.combine_value(c, {**num, "year": 2015}, den, {"sex": "계"})
    assert other_year["status"] == "missing_data" and "연도가 다릅니다" in other_year["reason"]


def test_compute_hands_dynamic_side_to_ask_session(no_db) -> None:
    cross = {"num": "1인 가구 수", "den": "총인구", "scale": 1000, "dynamic": "num"}
    q = {"type": "value", "indicator": "인구 1,000명당 1인 가구 수", "regions": ["서울"]}
    q["cross"] = cross
    assert qa.compute(q)["status"] == qa.CROSS_DYNAMIC


def test_cross_dynamic_divides(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from app.services import ask_session, dynamic_indicator, table_guide

    asked = []

    async def cached(question, parsed, trace):
        asked.append(question)
        return None

    async def find(question, sid, selections, user_id):
        return {"status": "ok"}

    async def handoff(question, parsed, found, trace):
        result = {"status": "ok", "indicator": "1인 가구 수", "unit": "가구", "value": 1390701,
                  "region": "서울특별시", "year": 2020, "sex": "계", "source": "1인가구 표",
                  "basis": "인구총조사", "tbl_id": "DT_1PR2007"}  # fmt: skip
        return {"status": "ok", "result": result}

    def compute(q):
        assert q["indicator"] == "총인구" and q["year"] == 2020
        return {"status": "ok", "indicator": "총인구", "unit": "명", "value": 9668465,
                "region": "서울특별시", "year": 2020, "sex": "계", "source": "주민등록",
                "basis": "주민등록", "tbl_id": "DT_1B04006"}  # fmt: skip

    async def cards(res, asked):
        return None

    monkeypatch.setattr(dynamic_indicator, "cached_answer", cached)
    monkeypatch.setattr(table_guide, "find_tables", find)
    monkeypatch.setattr(dynamic_indicator, "compute_from_handoff", handoff)
    monkeypatch.setattr(qa, "compute", compute)
    monkeypatch.setattr(ask_session, "_attach_cards", cards)
    parsed = {"type": "value", "regions": ["서울특별시"], "year": 2020, "sex": "계",
              "cross": {"num": "1인 가구 수", "den": "총인구", "scale": 1000, "form": "per",
                        "dynamic": "num"}}  # fmt: skip
    res = {"status": qa.CROSS_DYNAMIC, "parsed": parsed, "request_type": "new"}
    assert asyncio.run(ask_session._cross_dynamic(res, None, "질문", None)) is True
    assert asked == ["2020년 서울특별시 1인 가구 수"]
    assert res["status"] == "ok" and res["result"]["value"] == 143.84
    assert "1인 가구 수 1,390,701가구 ÷ 총인구 9,668,465명" in res["answer"]


def test_cross_dynamic_trend_divides_each_year(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from app.services import ask_session, dynamic_indicator, table_guide

    asked = []

    async def cached(question, parsed, trace):
        asked.append((question, parsed["type"]))
        series = [{"year": 2015, "value": 1000}, {"year": 2020, "value": 1500}]
        result = {"status": "ok", "indicator": "1인 가구 수", "unit": "가구", "series": series,
                  "region": "서울특별시", "sex": "계", "source": "1인가구 표",
                  "basis": "인구총조사", "tbl_id": "DT_1PR2007"}  # fmt: skip
        return {"status": "ok", "result": result}

    def compute(q):
        assert q["indicator"] == "총인구" and q["type"] == "trend"
        series = [{"year": 2015, "value": 10000}, {"year": 2020, "value": 10000}]
        return {"status": "ok", "indicator": "총인구", "unit": "명", "series": series,
                "region": "서울특별시", "sex": "계", "source": "주민등록", "basis": "주민등록",
                "tbl_id": "DT_1B04006"}  # fmt: skip

    async def cards(res, asked):
        return None

    async def no_search(*a, **k):
        raise AssertionError("캐시에 있으면 통계표 찾기를 부르지 않는다")

    monkeypatch.setattr(dynamic_indicator, "cached_answer", cached)
    monkeypatch.setattr(table_guide, "find_tables", no_search)
    monkeypatch.setattr(qa, "compute", compute)
    monkeypatch.setattr(ask_session, "_attach_cards", cards)
    parsed = {"type": "trend", "regions": ["서울특별시"], "start_year": 2015, "end_year": 2020,
              "sex": "계", "cross": {"num": "1인 가구 수", "den": "총인구", "scale": 1000,
                                     "form": "per", "dynamic": "num"}}  # fmt: skip
    res = {"status": qa.CROSS_DYNAMIC, "parsed": parsed}
    assert asyncio.run(ask_session._cross_dynamic(res, None, "질문", None)) is True
    assert asked == [("2015년부터 2020년까지 서울특별시 1인 가구 수 추이", "trend")]
    assert [s["value"] for s in res["result"]["series"]] == [100.0, 150.0]


def test_side_question_by_kind() -> None:
    from app.services import ask_session as s

    p = {"regions": ["서울특별시", "부산광역시"], "year": 2020, "level": "시군구"}
    assert s._side_question(p, "1인 가구 수", "compare") == (
        "2020년 서울특별시 부산광역시 1인 가구 수 비교"
    )
    assert s._side_question(p, "1인 가구 수", "rank") == "2020년 시군구별 1인 가구 수"
    assert s._side_question(p, "1인 가구 수", "value") == "2020년 서울특별시 1인 가구 수"


@pytest.mark.needs_data
def test_detect_both_unknown_only_on_request() -> None:
    q = "2020년 서울 1인 가구 중 통근하는 사람 비율"
    assert x.detect_partial(q) is None
    c = x.detect_partial(q, both=True)
    assert (c.num, c.den, c.scale, c.dynamic) == ("통근하는 사람", "1인 가구", 100, "both")


def test_bare_per_people_means_population() -> None:
    assert x.detect("인구 1000명당 혼인 건수는?").official == "조혼인율"
    c = x.detect("2024년 서울 1000명당 외국인근로자 수")
    assert (c.num, c.den, c.scale) == ("외국인근로자 수", "총인구", 1000)


def test_detect_summed_numerator() -> None:
    c = x.detect("2024년 서울 인구 1,000명당 혼인 건수와 이혼 건수 합")
    assert (c.num, c.plus, c.den, c.scale) == ("혼인 건수", "이혼 건수", "총인구", 1000)
    assert c.name == "인구 1,000명당 혼인 건수와 이혼 건수"
    assert c.formula == "(혼인 건수 + 이혼 건수) ÷ 총인구 × 1,000"
    assert c.official is None
    s = x.detect("세대당 출생아 수랑 사망자 수 합친 것")
    assert (s.num, s.plus, s.den) == ("출생아 수", "사망자 수", "세대수")
    assert x.detect("서울 출생아 수와 사망자 수") is None


def test_summed_numerator_value_trend_rank() -> None:
    def run(q):
        res = fake_run()(q)
        if q["indicator"] == "사망자 수" and q["type"] == "value":
            res = {**res, "value": 46000}
        return res

    q = q_of("value", year=2024)
    q["cross"]["plus"] = "사망자 수"
    res = x.compute(q, run)
    assert res["status"] == "ok"
    assert res["operands"][0] == {"label": "출생아 수와 사망자 수", "value": 87605, "unit": "명"}
    assert res["value"] == x._div(41605 + 46000, 4482063, 1)

    t = q_of("trend")
    t["cross"]["plus"] = "출생아 수"
    assert [s["year"] for s in x.compute(t, fake_run())["series"]] == [2024, 2025]

    r = q_of("rank", year=2024, top=1)
    r["cross"]["plus"] = "출생아 수"
    assert x.compute(r, fake_run())["ranking"] == [
        {"region": "부산광역시", "value": 10.0, "rank": 1}
    ]


def test_monthly_value_is_not_mixed_in_any_kind() -> None:
    def run(q):
        res = fake_run()(q)
        return {**res, "as_of": "2026년 10월 말"} if q["indicator"] == "세대수" else res

    res = x.compute(q_of("compare", year=2024, regions=["서울특별시", "부산광역시"]), run)
    assert res["status"] == "missing_data" and "최신 달" in res["reason"]


def test_reference_date_note() -> None:
    c = x.Cross("외국인주민 수", "총인구", 1000)
    num = {"basis": "지방자치단체 외국인주민 현황, 각 연도 11월 1일"}
    den = {"basis": "주민등록인구, 각 연도 12월 31일 기준"}
    assert (
        "기준 시점이 다릅니다 (외국인주민 수 11월 1일, 총인구 12월 31일)"
        in x._reference_note(c, num, den)[0]
    )
    flow = {"basis": "인구동향조사, 각 연도 1월 1일 ~ 12월 31일"}
    assert x._reference_note(c, flow, den) == []


def test_correct_summed_numerator_is_not_period_sum() -> None:
    parsed = {"type": "trend", "indicator": "출생아 수", "aggregate": "sum"}
    qa._cross(
        "2020년부터 2024년까지 서울 세대당 출생아 수와 사망자 수 합친 것 추이",
        parsed,
        lambda k, v, r: parsed.update({k: v}),
    )
    assert parsed["cross"]["plus"] == "사망자 수" and "aggregate" not in parsed


@pytest.mark.parametrize(
    ("question", "num", "den", "form"),
    [
        ("2024년 서울 혼인 건수와 이혼 건수 차이는?", "혼인 건수", "이혼 건수", "diff"),
        ("2024년 서울 혼인 건수가 이혼 건수보다 몇 건 많아?", "혼인 건수", "이혼 건수", "diff"),
        ("2024년 서울 전입자 수에서 전출자 수를 빼면?", "전입자 수", "전출자 수", "diff"),
        ("2024년 서울 혼인 건수는 이혼 건수의 몇 배야?", "혼인 건수", "이혼 건수", "versus"),
    ],
)
def test_detect_diff_and_times(question: str, num: str, den: str, form: str) -> None:
    c = x.detect(question)
    assert (c.num, c.den, c.form, c.scale) == (num, den, form, 1)


def test_diff_names_and_official() -> None:
    c = x.Cross("혼인 건수", "이혼 건수", 1, "diff")
    assert c.name == "혼인 건수와 이혼 건수의 차이"
    assert (c.unit, c.formula, c.verb) == ("건", "혼인 건수 − 이혼 건수", "뺄")
    assert x.detect("2024년 서울 출생아 수와 사망자 수 차이는?").official == "자연증가 건수"
    assert x.detect("2024년 서울 혼인 건수와 출생아 수 차이") is None


def test_diff_value_and_trend() -> None:
    q = q_of("value", num="출생아 수", den="세대수", year=2024)
    q["cross"]["form"] = "diff"
    res = x.compute(q, fake_run())
    assert res["value"] == 41605 - 4482063 and res["op"] == "−" and "scale" not in res
    t = q_of("trend", num="출생아 수", den="세대수")
    t["cross"]["form"] = "diff"
    series = x.compute(t, fake_run())["series"]
    assert series[0] == {"year": 2024, "value": 41605 - 4482063}


def test_operand_line_shows_scale_and_minus() -> None:
    per = {"value": 4.04, "unit": "명", "kind": "value", "scale": 1000, "operands": [
        {"label": "외국인근로자 수", "value": 37734, "unit": "명"},
        {"label": "총인구", "value": 9331828, "unit": "명"}]}  # fmt: skip
    assert "× 1,000 = 4.04명" in qa._body(per)[0]
    diff = {"value": 30317, "unit": "건", "kind": "value", "op": "−", "operands": [
        {"label": "혼인 건수", "value": 42471, "unit": "건"},
        {"label": "이혼 건수", "value": 12154, "unit": "건"}]}  # fmt: skip
    assert "혼인 건수 42,471건 − 이혼 건수 12,154건 = 30,317건" in qa._body(diff)[0]


def test_within_ratio_needs_part_of_whole() -> None:
    c = x.Cross("통근하는 사람", "1인 가구", 100, "within")
    num = {"value": 4486774, "unit": "명", "year": 2020, "region": "서울", "sex": "계"}
    den = {"value": 1390701, "unit": "가구", "year": 2020, "region": "서울", "sex": "계"}
    res = x.combine_value(c, num, den, {"sex": "계"})
    assert res["status"] == "missing_data" and "단위가 달라" in res["reason"]
    bigger = x.combine_value(c, {**num, "unit": "가구"}, den, {"sex": "계"})
    assert bigger["status"] == "missing_data" and "일부가 아닙니다" in bigger["reason"]
    part = x.combine_value(c, {**num, "unit": "가구", "value": 139070}, den, {"sex": "계"})
    assert part["status"] == "ok" and part["value"] == 10.0


def test_numerator_right_before_denominator_versus() -> None:
    c = x.detect("작년에 이사한 사람 인구 대비 몇 퍼센트야?")
    assert (c.num, c.den, c.scale, c.form) == ("이동자 수", "총인구", 100, "versus")


def test_value_without_region_is_nationwide() -> None:
    seen = []

    def run(q):
        seen.append(tuple(q.get("regions") or []))
        return fake_run()(q)

    q = q_of("value", year=2024)
    q["regions"] = []
    x.compute(q, run)
    assert set(seen) == {("전국",)}


@pytest.mark.needs_data
def test_within_with_jung_e() -> None:
    c = x.detect_partial("등록 외국인 중에 베트남 사람 비율 얼마나 돼?")
    assert (c.num, c.den, c.form, c.dynamic) == ("베트남 사람", "등록외국인 수", "within", "num")


def test_unknown_side_tried_in_known_tables_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from app.services import ask_session, dynamic_indicator, indicators

    tried = []

    async def fake_try(tbl, org, question, parsed, trace, forced, *a, **k):
        tried.append((tbl, question))
        result = {"status": "ok", "value": 293907, "unit": "명", "year": 2025, "tbl_id": tbl}
        return {"status": "ok", "result": result}, None

    monkeypatch.setattr(dynamic_indicator, "_try", fake_try)
    spec = {"method": "lookup", "sources": [{"table": "DT_1B040A9C"}]}
    monkeypatch.setattr(indicators, "spec", lambda n: spec)
    q = {"regions": ["전국"], "year": 2025}
    got = asyncio.run(ask_session._same_table_side("베트남 사람", "등록외국인 수", q, "value", {}))
    assert got["value"] == 293907 and tried == [("DT_1B040A9C", "2025년 전국 베트남 사람")]


def test_sum_of_two_indicators() -> None:
    c = x.detect("서울 인구, 등록 외국인도 포함해서 총 몇 명이야?")
    assert (c.num, c.den, c.form) == ("총인구", "등록외국인 수", "sum")
    assert c.name == "총인구와 등록외국인 수의 합" and c.formula == "총인구 + 등록외국인 수"
    res = x.compute(q_of("value", num="출생아 수", den="세대수", year=2024) | {"cross": {
        "num": "출생아 수", "den": "세대수", "scale": 1, "form": "sum"}}, fake_run())  # fmt: skip
    assert res["value"] == 41605 + 4482063 and res["op"] == "+"


@pytest.mark.needs_data
def test_people_word_is_total_population() -> None:
    c = x.detect_partial("서울 사람 중 외국인 주민은 몇 퍼센트야?")
    assert (c.num, c.den, c.scale, c.official) == ("외국인주민 수", "총인구", 100, None)


def test_population_denominator_is_percent() -> None:
    c = x.detect("인구 대비 등록외국인 많은 시도 세 군데")
    assert c.scale == 100 and c.official == "등록외국인 비율"
    assert x.detect("2024년 서울 출생아 수 대비 사망자 수").scale == 1


@pytest.mark.needs_data
def test_rank_without_year_aligns_years() -> None:
    asked = []

    def run(q):
        asked.append((q["indicator"], q.get("year"), (q.get("asked") or {}).get("latest")))
        res = fake_run()(q)
        if q["indicator"] == "세대수" and not q.get("year"):
            return {**res, "year": 2026, "as_of": "2026년 10월 말"}
        return res

    q = q_of("rank", top=1)
    q["asked"] = {"latest": True}
    res = x.compute(q, run)
    assert res["status"] == "ok"
    assert ("출생아 수", 2024, False) in asked and ("세대수", 2024, False) in asked


def _surveys(monkeypatch, by_table: dict) -> None:
    from app.services import catalog

    def surveys(ids):
        return {t: by_table[t] for t in ids if t in by_table}

    monkeypatch.setattr(catalog, "surveys", surveys)
    monkeypatch.setattr(catalog, "_groups", lambda ids: {})


def test_diff_of_different_surveys_is_side_by_side(monkeypatch) -> None:
    _surveys(monkeypatch, {"REG": "주민등록인구현황", "CENSUS": "인구총조사"})
    c = x.Cross("총인구", "인구총조사 인구", 1, "diff")
    num = {"status": "ok", "indicator": "총인구", "kind": "count", "value": 51217221,
           "unit": "명", "year": 2024, "region": "전국", "sex": "계", "tbl_id": "REG"}  # fmt: skip
    den = {**num, "indicator": "인구총조사 인구", "value": 51774521, "tbl_id": "CENSUS"}
    res = x.combine_value(c, num, den, {"sex": "계"})
    assert res["status"] == "ok" and res["split_by"] == "indicator" and "value" not in res
    assert [p["value"] for p in res["split"]] == [51217221, 51774521]
    said = "총인구는 주민등록인구현황, 인구총조사 인구는 인구총조사 통계입니다"
    assert said in res["basis_gap"] and "직접 비교하지 않습니다" in qa.render(res)
    assert "557,300" not in qa.summary(res)
    same = x.combine_value(c, num, {**den, "tbl_id": "REG"}, {"sex": "계"})
    assert same["status"] == "ok" and same["value"] == -557300


def test_ratio_of_different_surveys_still_divides(monkeypatch) -> None:
    _surveys(monkeypatch, {"REG": "주민등록인구현황", "FOREIGN": "지방자치단체외국인주민현황"})
    c = x.Cross("외국인주민 수", "총인구", 100, "within")
    num = {"value": 100, "unit": "명", "year": 2024, "region": "서울", "sex": "계",
           "tbl_id": "FOREIGN"}  # fmt: skip
    den = {**num, "value": 1000, "tbl_id": "REG"}
    assert x.combine_value(c, num, den, {"sex": "계"})["value"] == 10.0


def test_basis_gap_does_not_block_without_db(monkeypatch) -> None:
    from app.core.stats_db import StatsNotLoadedError
    from app.services import catalog

    def down(ids):
        raise StatsNotLoadedError("DB 없음")

    monkeypatch.setattr(catalog, "surveys", down)
    assert x.basis_gap({"tbl_id": "A"}, {"tbl_id": "B"}, "가", "나") is None


def test_change_and_compare_use_unrounded_values():
    c = x.Cross(num="등록외국인 수", den="총인구", scale=100, form="within")
    raw = [x._calc_raw(c, 39420, 3350380), x._calc_raw(c, 60929, 3241600)]
    series = [{"year": 2021, "value": x._round(raw[0])}, {"year": 2025, "value": x._round(raw[1])}]
    assert [s["value"] for s in series] == [1.18, 1.88]
    change = x._change(series, c, raw=raw)
    assert change["relative_rate"] == 59.8
    assert change["diff"] == 0.703

    vals = [{"region": "가", "value": 1.18}, {"region": "나", "value": 1.88}]
    facts = x._compare_facts(vals, "%", raw={"가": raw[0], "나": raw[1]})
    assert facts["ratio"]["value"] == 1.6
    assert facts["gap"] == 0.703


def test_change_gives_cagr_from_unrounded_values():
    c = x.Cross(num="출생아 수", den="총인구", scale=1000, form="per")
    raw = [x._calc_raw(c, 10000, 1000000), x._calc_raw(c, 8000, 1100000)]
    series = [{"year": 2015, "value": x._round(raw[0])}, {"year": 2025, "value": x._round(raw[1])}]
    change = x._change(series, c, raw=raw)
    assert change["rate"] == -27.27
    assert change["cagr"] == -3.1
