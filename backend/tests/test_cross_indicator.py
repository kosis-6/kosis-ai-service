"""두 지표를 나눈 값 (cross_indicator.py): 표현 찾기, 공식 지표로 보내기, 나누기 전 검사"""

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
        # 공식 지표가 있는 조합은 그 지표로 (분모가 연앙인구라 직접 나누면 공표 값과 다르다)
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
        "서울 출생아 수와 사망자 수",  # 함께 묻기 (나누기가 아님)
        "고령인구 비율 대비 출생아 수",  # 비율 지표는 재료로 쓰지 않는다
        "서울 출생아 수",  # 지표 하나
        "출생아 수 중 사망자 수",  # '중'은 비율을 물을 때만
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
    # 세대당 출생아 0.0093명을 0.01로 뭉개지 않는다
    assert x._div(41605, 4482063, 1) == 0.00928
    assert x._div(37734, 9331828, 1000) == 4.04
    assert x._div(1, 0, 1) is None


# ---- 계산 (가짜 qa.compute) ------------------------------------------------------------------

VALUES = {  # (지표, 연도) → 값
    ("출생아 수", 2024): 41605,
    ("출생아 수", 2025): 45516,
    ("세대수", 2024): 4482063,
    ("세대수", 2025): 4501031,
    ("사망자 수", 2024): 46000,
}


def fake_run(latest: dict | None = None, sexless: tuple = ()):
    """qa.compute 대신. latest: 연도를 말하지 않았을 때 지표마다 답하는 연도"""
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
    # 연도를 말하지 않아 출생아 수는 2024년, 세대수는 2025년으로 답했다 → 둘 다 있는 2024년으로
    run = fake_run(latest={"출생아 수": 2024, "세대수": 2025})
    res = x.compute(q_of("value"), run)
    assert res["status"] == "ok" and res["year"] == 2024
    assert ("출생아 수", 2024) in run.calls and ("세대수", 2024) in run.calls


def test_part_failure_is_passed_on_with_reason() -> None:
    res = x.compute(q_of("value", year=2030), fake_run())
    assert res["status"] == "out_of_range"
    assert "세대당 출생아 수를 계산하려면 출생아 수가 필요합니다" in res["reason"]


def test_sex_not_split_is_refused() -> None:
    # 세대수는 성별로 나뉘지 않는다: 여자 출생아 수 ÷ 전체 세대수로 답하지 않는다
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
    # 서울 100 ÷ 40 = 2.5, 부산 50 ÷ 10 = 5 → 부산이 높다 (분자 순서와 다르게)
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


# ---- qa.correct에 붙는 곳 --------------------------------------------------------------------


def test_correct_marks_cross_and_fixes_hcx_indicator() -> None:
    # HCX가 "세대당 출생아 수"를 출생아 수로 읽어도 두 지표를 나눈 값으로 고친다
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
    # HCX가 '대비'를 지역 비교로 읽었는데 지역이 하나: 값 질문으로
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


# ---- 한쪽만 사전에 있는 경우 (detect_partial · ask_session._cross_dynamic) -------------------


@pytest.fixture
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """지역 이름 확인 · 자료 연도를 DB 없이"""
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
        "2024년 서울 세대당 출생아 수",  # 둘 다 사전 지표 (detect가 맡는다)
        "2020년 서울 1인 가구 중 여성 비율",  # 둘 다 사전에 없음 (한 표 안의 동적 지표)
        "2020년 서울 1인 가구 수",  # 나누기 아님
    ],
)
def test_detect_partial_none(no_db, question) -> None:
    assert x.detect_partial(question) is None


def test_plain_words_keep_dictionary_indicator() -> None:
    # "여자 인구"는 총인구(성별은 조건), "통근 인구"는 다른 통계
    assert x._side("여자 인구") == ("총인구", None)
    assert x._side("통근 인구") == (None, "통근 인구")
    assert x._side("1인 가구 수는") == (None, None)  # 조사는 _right_phrase가 뗀다
    assert x._right_phrase(["1인", "가구", "수는"]) == "1인 가구 수"


def test_official_unmarried_ratio(no_db) -> None:
    # 공식 미혼 비율은 분모가 15세 이상 인구라 총인구로 나누지 않는다
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
    assert asked == ["2020년 서울특별시 1인 가구 수"]  # 사전에 없는 쪽만 따로 물은 것처럼
    assert res["status"] == "ok" and res["result"]["value"] == 143.84
    assert "1인 가구 수 1,390,701가구 ÷ 총인구 9,668,465명" in res["answer"]


def test_cross_dynamic_only_for_one_region_value() -> None:
    import asyncio

    from app.services import ask_session

    parsed = {"type": "trend", "regions": ["서울특별시"],
              "cross": {"num": "1인 가구 수", "den": "총인구", "scale": 1000,
                        "dynamic": "num"}}  # fmt: skip
    res = {"status": qa.CROSS_DYNAMIC, "parsed": parsed}
    assert asyncio.run(ask_session._cross_dynamic(res, None, "질문", None)) is False


def test_bare_per_people_means_population() -> None:
    # "1000명당 혼인 건수"처럼 분모를 말하지 않으면 인구 1,000명당 → 공식 조혼인율
    assert x.detect("1000명당 혼인 건수 얼마나 돼?").official == "조혼인율"
    c = x.detect("2024년 서울 1000명당 외국인근로자 수")
    assert (c.num, c.den, c.scale) == ("외국인근로자 수", "총인구", 1000)


# ---- 3단계 마무리: 분자 두 지표의 합 (표 3개), 주기 · 기준 시점 ------------------------------


def test_detect_summed_numerator() -> None:
    c = x.detect("2024년 서울 인구 1,000명당 혼인 건수와 이혼 건수 합")
    assert (c.num, c.plus, c.den, c.scale) == ("혼인 건수", "이혼 건수", "총인구", 1000)
    assert c.name == "인구 1,000명당 혼인 건수와 이혼 건수"
    assert c.formula == "(혼인 건수 + 이혼 건수) ÷ 총인구 × 1,000"
    assert c.official is None  # 합은 공식 지표가 아니다
    s = x.detect("세대당 출생아 수랑 사망자 수 합친 것")
    assert (s.num, s.plus, s.den) == ("출생아 수", "사망자 수", "세대수")
    assert x.detect("서울 출생아 수와 사망자 수") is None  # 나누기가 아님


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
    t["cross"]["plus"] = "출생아 수"  # 같은 지표 둘이어도 연도별로 더한다
    assert [s["year"] for s in x.compute(t, fake_run())["series"]] == [2024, 2025]

    r = q_of("rank", year=2024, top=1)
    r["cross"]["plus"] = "출생아 수"
    # 서울 (100+100) ÷ 40 = 5, 부산 (50+50) ÷ 10 = 10
    assert x.compute(r, fake_run())["ranking"] == [
        {"region": "부산광역시", "value": 10.0, "rank": 1}
    ]


def test_monthly_value_is_not_mixed_in_any_kind() -> None:
    # 연간 값과 최신 달 값은 값 질문이 아니어도 섞지 않는다
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
    assert x._reference_note(c, flow, den) == []  # 기간 값 ÷ 시점 값은 보통이다


def test_correct_summed_numerator_is_not_period_sum() -> None:
    parsed = {"type": "trend", "indicator": "출생아 수", "aggregate": "sum"}
    qa._cross(
        "2020년부터 2024년까지 서울 세대당 출생아 수와 사망자 수 합친 것 추이",
        parsed,
        lambda k, v, r: parsed.update({k: v}),
    )
    assert parsed["cross"]["plus"] == "사망자 수" and "aggregate" not in parsed
