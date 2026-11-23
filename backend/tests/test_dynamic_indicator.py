"""동적 지표 계산 1단계 (dynamic_indicator.py): 표의 칸으로 지표 정의 만들기

DB 없이: 1인가구 표(DT_1PL1502)의 칸 목록을 그대로 흉내 낸다.
  항목 T00 1인가구 / T10 남자 / T20 여자, c1 행정구역, c2 연령별 (000 합계, 024 20세미만 …)
"""

import asyncio

import pytest

from app.services import dynamic_indicator as d
from app.services import indicators, lookup, qa, search
from app.services import slots as search_slots

AGES = {
    "000": "합계",
    "024": "20세미만",
    "025": "20~24세",
    "030": "25~29세",
    "065": "60~64세",
    "070": "65~69세",
    "075": "70~74세",
    "080": "75~79세",
    "085": "80~84",
    "086": "85세이상",
}
ONE_PERSON = [
    d.Dim(0, "항목", {"T00": "1인가구", "T10": "남자", "T20": "여자"}),
    d.Dim(1, "행정구역별(시군구)", {"00": "전국", "11": "서울특별시"}),
    d.Dim(2, "연령별", AGES),
]
BASE = {"type": "value", "regions": ["서울"], "year": 2020, "sex": "계", "indicator": "1인 가구 수"}


@pytest.fixture(autouse=True)
def fake_table(monkeypatch: pytest.MonkeyPatch) -> dict:
    tables = {"DT_1PL1502": (ONE_PERSON, 1, "시군구")}
    units = {"DT_1PL1502": ("가구", True)}
    monkeypatch.setattr(d, "schema", lambda tbl_id: tables[tbl_id])
    monkeypatch.setattr(d, "_unit", lambda tbl_id, item, cells=None: units[tbl_id])
    monkeypatch.setattr(lookup, "_dim_labels", lambda tbl_id, dim: tuple(sorted(AGES.items())))
    monkeypatch.setattr(indicators, "find_regions", lambda text: [])
    projection: set[str] = set()
    monkeypatch.setattr(d, "is_projection", lambda tbl_id: tbl_id in projection)
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))

    async def no_remember(*args):  # 계산 방법 캐시(DB)는 test_plan_cache.py에서 본다
        return None

    monkeypatch.setattr(d, "_remember", no_remember)
    # 분자·분모 관계 검사: 지역·연도마다 (분자, 분모). 기본은 맞는 관계
    rows = {"rows": [(1.0, 2.0), (3.0, 3.0)]}
    monkeypatch.setattr(d, "_part_rows", lambda *args: rows["rows"])
    monkeypatch.setattr(lookup, "_table", lambda tbl_id: {"tbl_nm": "성 및 연령별 1인가구"})
    d.CHECKS.clear()
    return {"tables": tables, "units": units, "projection": projection, "rows": rows}


def build(question: str, **over) -> dict:
    spec, why = d.build("DT_1PL1502", question, {**BASE, **over})
    assert spec is not None, why
    return spec


def test_value_picks_item_by_name_and_totals_elsewhere() -> None:
    spec = build("2020년 서울 1인 가구 수")
    src = spec["sources"][0]
    assert spec["name"] == "1인 가구 수" and spec["unit"] == "가구" and spec["kind"] == "count"
    assert src["item_id"] == "T00" and src["level"] == "시군구"
    # 연령은 lookup이 질문마다 고른다 (말하지 않으면 합계)
    assert (src["age_dim"], src["age_total"], src["age_min"]) == ("c2", "000", 0)
    # 성별 칸이 항목에 있다: 성별마다 항목을 바꾼다
    assert src["by_sex"] == {
        "계": {"item_id": "T00"},
        "남": {"item_id": "T10"},
        "여": {"item_id": "T20"},
    }
    assert spec["dynamic"] and d.NOTE == spec["note"]


def test_ratio_of_sex_within_table() -> None:
    spec = build("서울 1인 가구 중 여성 비율", indicator="1인 가구 중 여성 비율", sex="여")
    src = spec["sources"][0]
    assert spec["kind"] == "ratio" and spec["unit"] == "%"
    assert (src["item_id"], src["den_item_id"]) == ("T20", "T00")
    assert "by_sex" not in src and spec["fixed"] == {"sex": "여", "ages": None}
    assert spec["formula"] == "여자 ÷ 1인가구 × 100"


def test_ratio_of_age_band_puts_ages_in_filters() -> None:
    spec = build(
        "서울 1인 가구 중 70세 이상 비율", indicator="70세 이상 1인 가구 비율", ages=[[70, None]]
    )
    src = spec["sources"][0]
    # "80~84"처럼 끝에 '세'가 없는 이름도 연령으로 읽는다
    assert src["filters"]["c2_id"] == ["075", "080", "085", "086"]
    assert src["den_filters"]["c2_id"] == "000" and src["den_item_id"] == "T00"
    assert "age_dim" not in src and spec["ages_in_filters"]


def test_age_ratio_uses_population_sex_on_both_sides() -> None:
    spec = build("서울 여성 1인 가구 중 70세 이상 비율", sex="여", ages=[[70, None]])
    src = spec["sources"][0]
    assert (src["item_id"], src["den_item_id"]) == ("T20", "T20")
    assert src["filters"]["c2_id"] == ["075", "080", "085", "086"]
    assert src["den_filters"]["c2_id"] == "000"
    assert spec["name"].startswith("여성 ")


@pytest.mark.parametrize(
    ("asked_age", "expected"),
    [(85, "086"), (65, ["075", "080", "085", "086"])],
)
def test_age_ratio_uses_subgroup_as_denominator(asked_age, expected) -> None:
    spec = build(
        f"서울 70세 이상 여성 1인 가구 중 {asked_age}세 이상 비율",
        sex="여",
        ages=[[70, None], [asked_age, None]],
    )
    src = spec["sources"][0]
    assert (src["item_id"], src["den_item_id"]) == ("T20", "T20")
    assert src["filters"]["c2_id"] == expected
    assert src["den_filters"]["c2_id"] == ["075", "080", "085", "086"]
    assert "÷ 70세 이상" in spec["formula"]


@pytest.mark.parametrize(("age", "lo"), [("70세 이상", 70), ("노인", 65)])
def test_sex_ratio_keeps_age_filter_on_both_sides(age, lo) -> None:
    spec = build(f"서울 {age} 1인 가구 중 여성 비율", sex="여", ages=[[lo, None]])
    src = spec["sources"][0]
    assert (src["item_id"], src["den_item_id"]) == ("T20", "T00")
    expected = ["075", "080", "085", "086"]
    if lo == 65:
        expected.insert(0, "070")
    assert src["filters"]["c2_id"] == src["den_filters"]["c2_id"] == expected


def test_composition_by_named_dimension() -> None:
    spec = build("서울 1인 가구 연령별 구성", type="composition")
    src = spec["sources"][0]
    assert spec["kind"] == "composition"
    assert (src["parts_dim"], src["parts_total"]) == ("c2", "000")
    assert "age_dim" not in src and spec["fixed"]["type"] == "composition"


def test_refuses_when_no_cell_matches_the_question() -> None:
    # 표의 다른 값(1인가구 합계)을 엉뚱한 질문의 답으로 내지 않는다
    spec, why = d.build("DT_1PL1502", "서울 통근 인구", BASE)
    assert spec is None and why == "질문의 말과 맞는 칸이 없음"


def test_asks_when_two_cells_match_in_one_dimension(fake_table: dict) -> None:
    # (세부 칸을 모두 말하면 합계라서, 칸을 하나 더 둔다)
    stay = d.Dim(3, "거주기간별", {"0": "계", "1": "1년미만", "2": "1~5년", "3": "6~10년"})
    fake_table["tables"]["DT_1PL1502"] = ([*ONE_PERSON, stay], 1, "시군구")
    spec, ask = d.build("DT_1PL1502", "서울 1년미만과 1~5년 1인 가구", BASE)
    assert spec is None and isinstance(ask, d.Ask)
    assert (ask.dim_no, ask.options) == (3, [("1", "1년미만"), ("2", "1~5년")])
    # 고른 칸으로 다시 만들면 그 칸
    spec, _ = d.build("DT_1PL1502", "서울 1년미만과 1~5년 1인 가구", BASE, {3: "2"})
    assert spec["sources"][0]["filters"]["c3_id"] == "2"


def test_asks_dimension_without_total(fake_table: dict) -> None:
    stay = d.Dim(3, "거주기간별", {"1": "1년 미만", "2": "1~5년"})
    fake_table["tables"]["DT_1PL1502"] = ([*ONE_PERSON, stay], 1, "시군구")
    spec, ask = d.build("DT_1PL1502", "서울 1인 가구 수", BASE)
    assert spec is None and ask.options == [("1", "1년 미만"), ("2", "1~5년")]


def test_too_many_cells_are_not_asked(fake_table: dict) -> None:
    many = d.Dim(3, "업종별", {str(i): f"업종{i}" for i in range(d.MAX_ASK_OPTIONS + 1)})
    fake_table["tables"]["DT_1PL1502"] = ([*ONE_PERSON, many], 1, "시군구")
    spec, why = d.build("DT_1PL1502", "서울 1인 가구 수", BASE)
    assert spec is None and why == "업종별에서 칸을 고르지 못함"


def test_no_match_is_not_asked(fake_table: dict) -> None:
    stay = d.Dim(3, "거주기간별", {"1": "1년 미만", "2": "1~5년"})
    fake_table["tables"]["DT_1PL1502"] = ([*ONE_PERSON, stay], 1, "시군구")
    spec, why = d.build("DT_1PL1502", "서울 통근 인구", BASE)  # 맞는 말이 없는 표는 묻지 않는다
    assert spec is None and why == "질문의 말과 맞는 칸이 없음"


def test_refuses_sex_when_table_has_none(fake_table: dict) -> None:
    dims = [d.Dim(0, "항목", {"A": "2인", "B": "3인"}), ONE_PERSON[1]]
    fake_table["tables"]["DT_1PL1502"] = (dims, 1, "시도")
    spec, why = d.build("DT_1PL1502", "서울 여자 2인 가구", {**BASE, "sex": "여"})
    assert spec is None and why == "표에 성별 구분이 없음"


def test_fractional_values_are_not_counts(fake_table: dict) -> None:
    dims = [d.Dim(0, "항목", {"A": "평균 가구원 수", "B": "가구"}), ONE_PERSON[1]]
    fake_table["tables"]["DT_1PL1502"] = (dims, 1, "시도")
    fake_table["units"]["DT_1PL1502"] = ("명", False)
    spec = build("2020년 전국 평균 가구원 수", indicator="평균 가구원 수")
    assert spec["kind"] == "value" and spec["name"] == "평균 가구원 수"


@pytest.mark.parametrize("term", ["2020년 서울", "전국 1인 가구 수"])
def test_name_ignores_terms_with_years_or_regions(monkeypatch, term: str) -> None:
    monkeypatch.setattr(indicators, "find_regions", lambda text: ["전국"] if "전국" in text else [])
    spec = build("2020년 서울 1인 가구 수", indicator=term, term=term)
    assert spec["name"] == "1인가구 수"


def test_dynamic_spec_is_reused_only_when_fixed_conditions_match() -> None:
    spec = build("서울 1인 가구 중 여성 비율", indicator="1인 가구 중 여성 비율", sex="여")
    q = {"indicator": spec["name"], "sex": "여", "dynamic_spec": spec}
    assert qa._dynamic_fits(q)
    assert not qa._dynamic_fits({**q, "sex": "남"})  # 여자 비율 정의를 "남자는?"에 쓰지 않는다
    assert not qa._dynamic_fits({**q, "indicator": "사망자 수"})  # 지표가 바뀜
    plain = build("2020년 서울 1인 가구 수")  # 성별마다 칸이 있는 정의는 성별이 바뀌어도 쓴다
    assert qa._dynamic_fits({"indicator": plain["name"], "sex": "남", "dynamic_spec": plain})


def test_with_spec_uses_table_last_year_when_year_not_asked(monkeypatch) -> None:
    monkeypatch.setattr(lookup, "_years", lambda src: (2015, 2025))
    spec = build("서울 1인 가구 수")
    q = d._with_spec({**BASE, "year": None}, spec)
    assert q["year"] == 2025 and q["dynamic_spec"] is spec and q["unsupported"] is False
    ratio = build("서울 1인 가구 중 여성 비율", sex="여", type="composition")
    assert d._with_spec({**BASE, "sex": "여", "type": "composition"}, ratio)["type"] == "value"


def test_answer_tries_ranked_tables_that_meet_all_conditions(monkeypatch) -> None:
    tried = []

    async def fake_build(tbl_id, org_id, question, parsed, step, forced=None, slots=None):
        tried.append(tbl_id)
        return (None, "질문의 말과 맞는 칸이 없음") if tbl_id == "A" else (build(question), None)

    async def fake_answer(q, trace, question):
        return {"status": "ok", "result": {"status": "ok", "value": 1390701}, "parsed": q}

    monkeypatch.setattr(d, "_build", fake_build)
    monkeypatch.setattr(qa, "answer", fake_answer)
    # 정답 표가 3순위여도 쓴다. 조건을 충족하지 못한(✘) 2순위 표는 건너뛴다
    recs = [
        {"tbl_id": "A", "checks": {"year": True}},
        {"tbl_id": "B", "checks": {"year": True, "sex": False}},
        {"tbl_id": "DT_1PL1502", "checks": {"year": True, "sex": True}},
    ]
    trace: dict = {}
    found = {"status": "ok", "recommendations": recs, "notice": "2020년 판 기준입니다"}
    res = asyncio.run(d.answer("2020년 서울 1인 가구 수", BASE, found, trace))
    assert tried == ["A", "DT_1PL1502"]
    skips = [step.get("skip", "") for step in trace["dynamic"]]
    assert skips[0] == "질문의 말과 맞는 칸이 없음" and skips[2] == ""
    assert skips[1] == "통계표 찾기 조건 불충족 (sex)"
    spec = res["parsed"]["dynamic_spec"]
    assert spec["name"] == "1인 가구 수" and spec["note"].endswith("2020년 판 기준입니다")
    # 되묻기·일부 일치·여러 표(함께 볼 표)는 계산하지 않는다
    for found in (
        {"status": "clarify"},
        {"status": "partial_match", "recommendations": recs[2:]},
        {"status": "ok", "recommendations": recs[2:], "together": [{"tbl_id": "B"}]},
    ):
        assert asyncio.run(d.answer("2020년 서울 1인 가구 수", BASE, found, {})) is None


# ---- 추계 표·결측 검증 -------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"year": 2020},  # 실적 자료가 있는 해
        {"year": None},  # 연도를 말하지 않음 → 추계 표의 마지막 해로 답하게 된다
        {"type": "trend", "year": None, "start_year": 2020, "end_year": 2040},  # 실적과 섞임
    ],
)
def test_projection_table_only_for_future_years(fake_table: dict, over: dict) -> None:
    fake_table["projection"].add("DT_1PL1502")
    spec, why = d.build("DT_1PL1502", "서울 1인 가구 수", {**BASE, **over})
    assert spec is None and why == "장래추계 표는 미래 연도를 물을 때만 씀"


def test_projection_table_for_future_year_and_not_reused_for_past(fake_table: dict) -> None:
    fake_table["projection"].add("DT_1PL1502")
    spec = build("2040년 서울 1인 가구 수", year=2040)
    assert spec["future_after"] == 2025 and d.PROJECTION_NOTE in spec["note"]
    q = {"indicator": spec["name"], "sex": "계", "year": 2040, "dynamic_spec": spec}
    assert qa._dynamic_fits(q)
    assert not qa._dynamic_fits({**q, "year": 2020})  # "2020년은?" → 실적 표로 다시 찾는다


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"status": "ok", "value": None}, False),  # 비공개(*)·결측
        ({"status": "ok", "series": [{"year": 2020, "value": None}]}, False),
        ({"status": "ok", "values": [{"region": "서울", "value": None}]}, False),
        ({"status": "ok", "value": 0}, True),  # 0은 값이다
        (
            {"status": "ok", "series": [{"year": 2020, "value": None}, {"year": 2021, "value": 3}]},
            True,
        ),
        ({"status": "ok", "parts": [{"label": "20대", "count": 1}]}, True),
        ({"status": "ok", "split": [{"value": None}, {"value": 5}]}, True),
    ],
)
def test_has_values(result: dict, expected: bool) -> None:
    assert d.has_values(result) is expected


def test_answer_without_values_falls_back(monkeypatch) -> None:
    async def fake_build(tbl_id, org_id, question, parsed, step, forced=None, slots=None):
        return build(question), None

    async def fake_answer(q, trace, question):  # 그 칸 값이 비공개
        return {"status": "ok", "result": {"status": "ok", "value": None}, "parsed": q}

    monkeypatch.setattr(d, "_build", fake_build)
    monkeypatch.setattr(qa, "answer", fake_answer)
    trace: dict = {}
    found = {"status": "ok", "recommendations": [{"tbl_id": "A"}]}
    assert asyncio.run(d.answer("2020년 서울 1인 가구 수", BASE, found, trace)) is None
    assert trace["dynamic"][0]["skip"] == "값이 없음 (비공개·결측)"


# ---- 칸 되묻기 --------------------------------------------------------------------


def test_answer_asks_when_no_table_computes(fake_table: dict, monkeypatch) -> None:
    stay = d.Dim(3, "거주기간별", {"1": "1년 미만", "2": "1~5년"})
    fake_table["tables"]["DT_1PL1502"] = ([*ONE_PERSON, stay], 1, "시군구")

    async def fake_build(tbl_id, org_id, question, parsed, step, forced=None, slots=None):
        return d.build(tbl_id, question, parsed, forced, slots)

    monkeypatch.setattr(d, "_build", fake_build)
    found = {"status": "ok", "recommendations": [{"tbl_id": "DT_1PL1502", "tbl_nm": "1인가구"}]}
    trace: dict = {}
    res = asyncio.run(d.answer("서울 1인 가구 수", BASE, found, trace))
    assert res["status"] == "clarify_question"
    c = res["clarification"]
    assert c["kind"] == "table_cell" and c["options"] == ["1년 미만", "1~5년"]
    assert c["prompt"] == "'1인가구' 표에서 거주기간별 중 어느 것을 볼까요?"
    assert c["context"]["codes"] == {"1년 미만": "1", "1~5년": "2"}
    assert c["context"]["dim_no"] == 3 and c["context"]["parsed"] == BASE
    assert "칸을 고르지 못함" in trace["dynamic"][0]["skip"]


def test_answer_choice_recomputes_with_picked_cell(fake_table: dict, monkeypatch) -> None:
    stay = d.Dim(3, "거주기간별", {"1": "1년 미만", "2": "1~5년"})
    fake_table["tables"]["DT_1PL1502"] = ([*ONE_PERSON, stay], 1, "시군구")
    seen = {}

    async def fake_build(tbl_id, org_id, question, parsed, step, forced=None, slots=None):
        return d.build(tbl_id, question, parsed, forced, slots)

    async def fake_answer(q, trace, question):
        seen["filters"] = q["dynamic_spec"]["sources"][0]["filters"]
        return {"status": "ok", "result": {"status": "ok", "value": 3}, "parsed": q}

    monkeypatch.setattr(d, "_build", fake_build)
    monkeypatch.setattr(qa, "answer", fake_answer)
    context = {"tbl_id": "DT_1PL1502", "dim_no": 3, "parsed": BASE, "forced": {}}
    res = asyncio.run(d.answer_choice("서울 1인 가구 수", context, "2", {}))
    assert res["status"] == "ok" and seen["filters"]["c3_id"] == "2"


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        ("1~5년", "2"),
        ("1 ~ 5년", "2"),
        ("2번", "2"),
        ("1", "1"),
        ("1년 미만으로", "1"),
        ("몰라", None),
    ],
)
def test_pick_option(reply: str, code: str | None) -> None:
    c = {"options": ["1년 미만", "1~5년"], "context": {"codes": {"1년 미만": "1", "1~5년": "2"}}}
    assert d.pick_option(reply, c) == code


def test_projection_scenario_defaults_to_middle(fake_table: dict) -> None:
    scenario = d.Dim(3, "추계별", {"1": "중위 추계(기본)", "2": "고위 추계", "3": "저위 추계"})
    fake_table["tables"]["DT_1PL1502"] = ([*ONE_PERSON, scenario], 1, "시군구")
    fake_table["projection"].add("DT_1PL1502")
    spec = build("2040년 서울 1인 가구 수", year=2040)
    assert spec["sources"][0]["filters"]["c3_id"] == "1"
    assert "추계별: 중위 추계(기본)" in spec["note"]  # 합계가 없는 시나리오는 중위
    # 시나리오를 말하면 그 칸
    spec = build("2040년 서울 고위 추계 1인 가구 수", year=2040)
    assert spec["sources"][0]["filters"]["c3_id"] == "2"


def test_implied_age_word_is_ignored_when_table_has_no_age(fake_table: dict) -> None:
    # "노년부양비"의 노년을 65세 이상으로 읽어도, 연령 분류가 없는 표에서는 지표 이름의 일부다
    items = d.Dim(0, "항목", {"A": "노년부양비", "B": "유소년부양비"})
    fake_table["tables"]["DT_1PL1502"] = ([items, ONE_PERSON[1]], 1, "시도")
    implied = {"ages": [[65, None]], "age_group": {"expr": "노년"}, "indicator": "노년부양비"}
    spec = build("2020년 서울 노년부양비", **implied)
    assert spec["sources"][0]["item_id"] == "A" and spec["ignore_ages"]
    # 숫자로 말한 연령은 지킨다
    spec, why = d.build("DT_1PL1502", "서울 65세 이상 노년부양비", {**BASE, "ages": [[65, None]]})
    assert spec is None and why == "표에 연령 구분이 없음"


# ---- 분자·분모 관계 검증 ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "ok"),
    [
        ([(5.0, 10.0), (10.0, 10.0)], True),  # 분자 ≤ 분모 (같아도 된다: 그 지역은 전부 여자)
        ([(5.0, 10.0), (11.0, 10.0)], False),  # 한 곳이라도 분자가 크면 부분이 아니다
        ([(None, 10.0), (None, None)], False),  # 함께 있는 값이 없다
        ([(10.4, 10.0)], True),  # 반올림 차이 (0.5 이내)는 넘어간다
    ],
)
def test_part_within_whole(fake_table: dict, rows, ok: bool) -> None:
    fake_table["rows"]["rows"] = rows
    src = {"table": "DT_1PL1502", "item_id": "T00"}
    assert (d.part_within_whole(src, "item_id", ["T20"], "T00") is None) is ok


def test_ratio_refused_when_part_is_not_within_whole(fake_table: dict) -> None:
    fake_table["rows"]["rows"] = [(12.0, 10.0)]
    spec, why = d.build("DT_1PL1502", "서울 1인 가구 중 여성 비율", {**BASE, "sex": "여"})
    assert spec is None and "분자가 분모보다 큰" in why


def test_age_band_sum_refused_when_cells_overlap(fake_table: dict) -> None:
    # 묶음 칸과 세부 칸이 함께 있어 더하면 연령 합계를 넘는다 → 숫자를 내지 않는다
    fake_table["rows"]["rows"] = [(12.0, 10.0)]
    spec, why = d.build("DT_1PL1502", "서울 70세 이상 1인 가구 수", {**BASE, "ages": [[70, None]]})
    assert spec is None and "분자가 분모보다 큰" in why


def test_composition_refused_when_parts_exceed_total(fake_table: dict) -> None:
    fake_table["rows"]["rows"] = [(15.0, 10.0)]  # 칸 안에 소계가 섞여 부분의 합이 합계를 넘는다
    spec, why = d.build("DT_1PL1502", "서울 1인 가구 연령별 구성", {**BASE, "type": "composition"})
    assert spec is None


def test_check_fixes_other_dimensions_like_the_definition(monkeypatch) -> None:
    seen = {}

    def rows(tbl_id, col, num, den, fixed, item):
        seen.update(col=col, fixed=fixed, item=item)
        return [(1.0, 2.0)]

    monkeypatch.setattr(d, "_part_rows", rows)
    d.CHECKS.clear()
    src = {
        "table": "T",
        "item_id": "T00",
        "filters": {"c3_id": "1"},
        "age_dim": "c2",
        "age_total": "000",
    }
    assert d.part_within_whole(src, "item_id", ["T20"], "T00") is None
    # 성별(항목)을 볼 때 나머지는 정의대로: 거주기간 1, 연령 합계
    assert seen == {"col": "item_id", "fixed": {"c3_id": "1", "c2_id": "000"}, "item": None}


# ---- 분류별 기본값 정책 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dim_name", "noted"),
    [
        ("거처의 종류별", False),  # 합계가 '전부'라는 뜻: 알리지 않는다
        ("국적별", False),
        ("특성별", True),  # 여러 기준이 섞인 분류: 합계로 했다고 알린다
        ("조사구분", True),  # 처음 보는 분류
    ],
)
def test_unspecified_dimension_total_policy(fake_table: dict, dim_name: str, noted: bool) -> None:
    extra = d.Dim(3, dim_name, {"0": "전체", "1": "가", "2": "나"})
    fake_table["tables"]["DT_1PL1502"] = ([*ONE_PERSON, extra], 1, "시군구")
    spec = build("2020년 서울 1인 가구 수")
    assert spec["sources"][0]["filters"]["c3_id"] == "0"
    assert (f"{dim_name}: 전체" in spec["note"]) is noted


# ---- 질문 조건이 칸에 모두 반영됐는가 (통계표 찾기의 조건) -------------------------------------


def slots(items=(), classes=(), exclude=()) -> dict:
    cond = {"items": list(items), "classes": list(classes)}
    return {"conditions": cond, "exclude": list(exclude)}


@pytest.mark.parametrize(
    ("question", "over", "found", "why"),
    [
        ("서울 1인 가구 수", {}, slots(items=["1인가구"]), None),
        ("서울 미혼 1인 가구 수", {}, slots(items=["1인가구", "미혼"]), "'미혼'"),  # 표에 없는 조건
        ("서울 70세 이상 1인 가구", {"ages": [[70, None]]}, slots(items=["70세 이상"]), None),
        ("서울 여자 1인 가구", {"sex": "여"}, slots(items=["여자"]), None),
        ("귀화자 빼고 1인 가구", {}, slots(exclude=["귀화자"]), "빼 달라는"),
        ("연령별 1인 가구 수", {}, slots(classes=["연령"]), "나눠 보기"),
        ("1인 가구 연령별 구성", {"type": "composition"}, slots(classes=["연령"]), None),
    ],
)
def test_conditions_must_be_reflected_in_cells(question, over, found, why) -> None:
    spec, reason = d.build("DT_1PL1502", question, {**BASE, **over}, None, found)
    if why is None:
        assert spec is not None, reason
    else:
        assert spec is None and why in reason


def test_implied_age_word_kept_out_when_not_in_cell(fake_table: dict) -> None:
    # "노인 1인 가구": 연령 분류가 없는 표에서 노인을 무시하면 전 연령 1인 가구가 된다
    # → 계산하지 않음
    items = d.Dim(0, "항목", {"A": "1인가구", "B": "2인가구"})
    fake_table["tables"]["DT_1PL1502"] = ([items, ONE_PERSON[1]], 1, "시도")
    implied = {"ages": [[65, None]], "age_group": {"expr": "노인"}}
    spec, why = d.build("DT_1PL1502", "서울 노인 1인 가구 수", {**BASE, **implied})
    assert spec is None and why == "질문의 연령 조건을 표의 칸에 반영할 수 없음"


# ---- 맞는 구간 없음 (숫자 조건이 표의 구간 분류와 맞지 않음) --------------------------------


def weight_table(fake_table: dict) -> None:
    weight = d.Dim(
        3, "출생시 체중별", {"0": "계", "1": "2.5㎏미만", "2": "2.5 - 2.9㎏", "3": "3.0 - 3.4㎏"}
    )
    items = d.Dim(0, "항목", {"B": "출생"})
    fake_table["tables"]["DT_1PL1502"] = ([items, ONE_PERSON[1], weight], 1, "시도")


@pytest.mark.parametrize(
    ("question", "item"),
    [
        (
            "2025년 체중 3.2kg으로 태어난 아기는 몇 명이야?",
            "3.2kg",
        ),  # 칸 글자가 안 맞아도 (출생 ≠ 아기)
        ("2025년 3.2 kg 출생", "3.2kg"),
    ],
)
def test_measure_condition_without_matching_band(
    fake_table: dict, question: str, item: str
) -> None:
    weight_table(fake_table)
    spec, miss = d.build("DT_1PL1502", question, {**BASE, "indicator": "출생아 수"})
    assert spec is None and isinstance(miss, d.BandMiss)
    # 표의 단위 기호(㎏)도 kg으로 맞춰 비교하고, 소수점은 지우지 않는다
    assert miss.item == item and miss.dim_name == "출생시 체중별"
    assert miss.labels == ["2.5㎏미만", "2.5 - 2.9㎏", "3.0 - 3.4㎏"]


def test_year_is_not_a_measure_condition(fake_table: dict) -> None:
    weight_table(fake_table)
    assert d._band_miss(["2025년 출생"], fake_table["tables"]["DT_1PL1502"][0], 1) is None


def test_spoken_age_range_counts_as_age_condition() -> None:
    # "62살에서 67살 사이"도 연령 조건으로 보고 lookup에 넘긴다 (구간이 안 맞으면 band_mismatch)
    found = {"conditions": {"items": ["62살에서 67살 사이"], "classes": []}, "exclude": []}
    spec, why = d.build(
        "DT_1PL1502",
        "서울 62살에서 67살 사이 1인 가구 수",
        {**BASE, "ages": [[62, 67]]},
        None,
        found,
    )
    assert spec is not None, why


@pytest.mark.parametrize(
    ("key", "ages", "expected"),
    [
        ("item:62살 이상", [[62, 67]], True),
        ("item:62세 이상 67세 이하", [[62, 67]], True),
        ("class:연령", [[62, 67]], True),
        ("item:미혼", [[62, 67]], False),
        ("sex", [[62, 67]], False),
        ("item:62살 이상", None, False),  # 질문에 연령 구간이 없으면 연령 조건으로 보지 않는다
    ],
)
def test_age_check(key: str, ages, expected: bool) -> None:
    assert d._age_check(key, {"ages": ages}) is expected


def test_partial_match_with_only_age_failures_is_computed(monkeypatch) -> None:
    # 통계표 찾기가 '62살 이상' 칸이 없다고 ✘를 줘도 연령 구간은 바로 계산(lookup)이 맡는다
    async def fake_build(tbl_id, org_id, question, parsed, step, forced=None, slots=None):
        return build(question, ages=[[62, 67]]), None

    async def fake_answer(q, trace, question):
        return {"status": "band_mismatch", "result": {"status": "band_mismatch"}, "parsed": q}

    monkeypatch.setattr(d, "_build", fake_build)
    monkeypatch.setattr(qa, "answer", fake_answer)
    age_only = {"year": True, "class:연령": True, "item:62살 이상": False}
    found = {"status": "partial_match", "recommendations": [{"tbl_id": "T", "checks": age_only}]}
    res = asyncio.run(
        d.answer("서울 62살에서 67살 사이 1인 가구 수", {**BASE, "ages": [[62, 67]]}, found, {})
    )
    assert res["status"] == "band_mismatch"
    other = {"year": True, "item:미혼": False}  # 연령 밖의 조건이 ✘면 계산하지 않는다
    found = {"status": "partial_match", "recommendations": [{"tbl_id": "T", "checks": other}]}
    assert (
        asyncio.run(d.answer("서울 미혼 1인 가구", {**BASE, "ages": [[62, 67]]}, found, {})) is None
    )


def test_total_suffix_names_match_plain_words(fake_table: dict) -> None:
    # "통학-계"처럼 이름 끝에 합계 표시가 붙은 항목도 "통학 인구"와 맞춘다
    items = d.Dim(0, "항목", {"A": "통근·통학-계", "B": "통학-계", "C": "통학-남자"})
    fake_table["tables"]["DT_1PL1502"] = ([items, ONE_PERSON[1]], 1, "시도")
    fake_table["units"]["DT_1PL1502"] = ("명", True)
    spec = build("2010년 전국 통학 인구는 몇 명이야?", indicator="통학 인구", term="통학 인구")
    assert spec["sources"][0]["item_id"] == "B" and spec["name"] == "통학 인구"


@pytest.mark.parametrize(
    ("term", "name"),
    [
        ("30대 미혼 인구", "미혼 인구"),
        ("62살에서 67살 사이 사망자 수", "사망자 수"),
        ("70세 이상 1인 가구 수", "1인 가구 수"),
        ("미혼 인구", "미혼 인구"),
    ],
)
def test_age_words_removed_from_name(term: str, name: str) -> None:
    # lookup이 연령 표현을 이름 앞에 붙이므로 ("30대 미혼 인구") 두 번 나오지 않게 뺀다
    assert d._without_age_words(term) == name


# ---- 한 분류 안의 칸들로 합·비교·순위 ------------------------------------------------------

MODES = {
    "T": "통근·통학 인구(12세 이상)",
    "S": "단일 수단-계",
    "W": "걸어서",
    "C": "승용차 소형 승합차",
    "B": "자전거",
}
VALUES = {"T": 30.0, "S": 28.0, "W": 7.0, "C": 9.0, "B": 1.0}


@pytest.fixture
def commute(fake_table: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    items = d.Dim(0, "항목", MODES)
    kind = d.Dim(2, "통근·통학여부별", {"0": "계", "1": "통근", "2": "통학"})
    fake_table["tables"]["DT_1PL1502"] = ([items, ONE_PERSON[1], kind], 1, "시도")
    fake_table["units"]["DT_1PL1502"] = ("명", True)
    monkeypatch.setattr(
        lookup,
        "_table",
        lambda tbl_id: {"tbl_nm": "소요시간 및 이용 교통수단별 통근 통학 인구(12세 이상)-시군구"},
    )
    monkeypatch.setattr(lookup, "_years", lambda src: (2010, 2020))

    def value(spec, region, year, sex="계", ages=None):
        code = spec["sources"][0]["item_id"]
        base = {"status": "ok", "unit": "명", "region": "전국", "source": "출처", "basis": "기준"}
        return {**base, "value": VALUES[code], "tbl_id": "DT_1PL1502", "url": "u"}

    monkeypatch.setattr(lookup, "value", value)


def answer_for(question: str, **over) -> dict:
    p = {**BASE, "regions": [], "year": 2010, **over}
    spec, why = d.build("DT_1PL1502", question, p)
    assert spec is not None and spec.get("parts_op"), why
    return d.parts_answer(p, spec, question)


@pytest.mark.usefixtures("commute")
def test_sum_of_two_cells_and_all_leaves_means_total() -> None:
    # "출퇴근"은 "통근"과 같은 말, 통근·통학을 모두 말하면 그 분류의 합계
    res = answer_for("2010년 걸어서나 자전거로 출퇴근 통학한 사람은 몇 명이야?")
    assert res["answer"].startswith(
        "2010년 전국의 걸어서·자전거 통근 통학 인구는 합해서 8명입니다."
    )
    assert res["parsed"]["dynamic_spec"]["sources"][0]["filters"]["c2_id"] == "0"


@pytest.mark.usefixtures("commute")
def test_compare_two_cells() -> None:
    res = answer_for("2010년 걸어서 통근한 사람과 자전거로 한 사람 차이는?")
    assert "걸어서가 자전거보다 6명 많습니다 (7.0배)" in res["answer"]


@pytest.mark.usefixtures("commute")
def test_rank_within_classification_skips_totals() -> None:
    res = answer_for("2010년 가장 많이 이용한 교통수단 top2는?", type="other")
    # 소계(단일 수단-계)는 이름으로, 합계(통근·통학 인구)는 값으로 알아보고 뺀다
    assert "교통수단 가운데" in res["answer"]
    assert "가장 많은 2개는 승용차 소형 승합차(9명), 걸어서(7명)입니다" in res["answer"]


@pytest.mark.usefixtures("commute")
def test_region_rank_is_not_category_rank() -> None:
    p = {**BASE, "regions": [], "year": 2010, "type": "rank"}
    spec, _ = d.build("DT_1PL1502", "2010년 걸어서 통근한 사람이 가장 많은 시도는?", p)
    assert spec is None or not spec.get("parts_op")


@pytest.mark.parametrize(
    ("question", "op"),
    [
        ("걸어서나 자전거로", "sum"),
        ("걸어서와 자전거 합계", "sum"),
        ("걸어서와 자전거 차이", "compare"),
        ("주간 인구가 상주 인구보다 몇 배", "compare"),
        ("걸어서와 자전거", None),
    ],
)
def test_multi_op(question: str, op) -> None:
    assert d._multi_op(question) == op


def test_set_code_keeps_sex_filter_of_separate_sex_dimension() -> None:
    # 성별이 따로 분류(c3)인 표: 항목을 바꿔도 성별 '계' 조건은 그대로 (빼면 계+남+여 = 두 배)
    by_sex = {"계": {"filters": {"c3_id": "0"}}, "남": {"filters": {"c3_id": "1"}}}
    spec = {"sources": [{"item_id": "T000", "by_sex": by_sex}], "parts_op": {"col": "item_id"}}
    assert d._set_code(spec, "item_id", "T101")["sources"][0]["by_sex"] == by_sex
    # 성별이 항목에 들어 있는 표: 항목을 바꾸면 성별 바꾸기는 뺀다
    in_item = {"계": {"item_id": "T00"}, "여": {"item_id": "T20"}}
    spec = {"sources": [{"item_id": "T00", "by_sex": in_item}], "parts_op": {"col": "item_id"}}
    assert "by_sex" not in d._set_code(spec, "item_id", "T10")["sources"][0]


# ---- 넘기기 꾸러미로 계산 (compute_from_handoff, 2026-11-17) ---------------------------


def _cand(tbl_id, rank, picked=False, checks=None, review=None, years=(2015, 2024), **over):
    return {
        "rank": rank,
        "tbl_id": tbl_id,
        "org_id": "101",
        "tbl_nm": f"{tbl_id} 표",
        "family_id": f"F{tbl_id}",
        "start_year": years[0],
        "end_year": years[1],
        "target": None,
        "sex": None,
        "source": "whole",
        "picked": picked,
        "review": review,
        "checks": checks if checks is not None else {},
        "editions": [],
        "members": [],
        "notes": [],
        **over,
    }


def _understanding(**over):
    u = {
        "conditions": {
            "region": None,
            "region_level": None,
            "year": 2020,
            "year_end": None,
            "sex": None,
            "items": [],
            "classes": [],
        },
        "age": None,
        "exclude": [],
        "target": None,
        "projection": False,
        "time_ref": None,
    }
    return {**u, **over}


def _found(cands, u=None, status="ok", combine=None, **over):
    target = {"span": None, "candidates": cands, "together": [], "conditions": {}}
    return {
        "status": status,
        "notice": None,
        "handoff": {
            "version": 1,
            "status": status,
            "understanding": u or _understanding(),
            "targets": [target],
            "combine": combine or {"op": "none", "note": None, "out_of_scope": []},
        },
        **over,
    }


@pytest.fixture
def computed(monkeypatch):
    """_build: FAIL에 든 표는 칸을 못 고름, 나머지는
    실제 build (DT_1PL1502 칸으로). qa.answer는 값 1"""
    state = {"fail": set(), "tried": []}

    async def fake_build(
        tbl_id, org_id, question, parsed, step, forced=None, slots=None, fetch_sem=None
    ):
        state["tried"].append(tbl_id)
        if (
            tbl_id in state["fail"] and not forced
        ):  # 칸을 정해 주면(되묻기 · 칸 고르기 LLM) 계산된다
            return None, "질문의 말과 맞는 칸이 없음"
        return d.build("DT_1PL1502", question, parsed, forced, slots)

    async def fake_answer(q, trace, question):
        return {"status": "ok", "result": {"status": "ok", "value": 1}, "parsed": q}

    async def no_offer(
        question, failures, alts
    ):  # 대안 판단 LLM은 부르지 않는다 (필요한 테스트가 바꾼다)
        state["judged"] = [a["entry"]["tbl_id"] for a in alts]
        return {"action": "tables", "offers": [], "reason": "테스트"}, "gemma"

    from app.services import alternative

    monkeypatch.setattr(d, "_build", fake_build)
    monkeypatch.setattr(qa, "answer", fake_answer)
    monkeypatch.setattr(alternative, "judge", no_offer)
    return state


Q1 = "2020년 서울 1인 가구 수"


def test_handoff_tries_candidates_and_keeps_priority(computed) -> None:
    computed["fail"] = {"A"}
    cands = [
        _cand("A", 1, picked=True, checks={"year": True}, review={"match": "partial"}),
        _cand("B", 2, checks={"year": False}),  # ✘
        _cand("C", 3, checks={}),  # 고른 표가 아닌데 확인한 조건이 없음
        _cand("D", 4, checks={"year": True}),
        _cand("E", 5, checks={"year": True}),
    ]
    trace: dict = {}
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace))
    assert res["status"] == "ok"
    # 동시에 계산해도 고르는 순서는 목록 순서: D와 E 모두 계산되지만 D
    assert trace["handoff"]["used"] == {"tbl_id": "D", "kind": "other"}
    assert sorted(computed["tried"]) == ["A", "D", "E"]
    skips = {s["tbl_id"]: s.get("skip") for s in trace["dynamic"]}
    assert skips["B"] == "통계표 찾기 조건 불충족 (year)"
    assert skips["C"] == "고른 표가 아닌데 확인한 조건이 없음"
    assert [s["tbl_id"] for s in trace["dynamic"]] == ["A", "B", "C", "D", "E"]  # 기록도 순서대로


def test_handoff_does_not_use_other_tables_when_review_is_exact(computed) -> None:
    # 검토가 '모두 충족'으로 고른 표가 계산되면 다른 후보는 계산하지 않는다
    cands = [
        _cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"}),
        _cand("D", 2, checks={"year": True}),
    ]
    trace: dict = {}
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace))
    assert res["status"] == "ok" and trace["handoff"]["used"]["tbl_id"] == "A"
    assert computed["tried"] == ["A"]
    assert trace["dynamic"][1]["skip"] == d.EXACT_SKIP


@pytest.mark.parametrize("have_full_table", [True, False])
@pytest.mark.parametrize("use_handoff", [True, False])
@pytest.mark.parametrize(
    ("question", "kind", "ages"),
    [
        ("2020년 서울 1인 가구 중 70세 이상 비율은?", "value", [[70, None]]),
        ("2020년 서울 전체 1인 가구 수", "value", None),
        ("2020년 서울 전체 1인 가구 연령별 구성", "composition", None),
    ],
)
def test_requires_the_requested_population(
    computed, have_full_table, use_handoff, question, kind, ages
) -> None:
    q = {**BASE, "type": kind, "ages": ages}
    limited = _cand(
        "A",
        1,
        picked=True,
        checks={"year": True},
        review={"match": "exact"},
        tbl_nm="성별·연령별 1인가구(20세 이상, 일반가구)",
    )
    cands = [limited]
    if have_full_table:
        cands.append(_cand("B", 2, checks={"year": True}, tbl_nm="성 및 연령별 1인가구"))
    trace = {}
    if use_handoff:
        res = asyncio.run(d.compute_from_handoff(question, q, _found(cands), trace))
    else:
        res = asyncio.run(d.answer(question, q, {"status": "ok", "recommendations": cands}, trace))
    assert res is not None
    if have_full_table:
        assert res["status"] == "ok"
        assert computed["tried"] == ["B"]
    else:
        assert res["status"] == "band_mismatch" and "value" not in res["result"]
        assert "20세 이상만 집계" in res["answer"] and res["sources"][0]["tbl_id"] == "A"
        assert computed["tried"] == []


@pytest.mark.parametrize(
    ("question", "limit"),
    [
        ("1인 가구 중 20세 이상 비율", "20세 이상"),
        ("18세 이상 1인 가구 중 70세 이상 비율", "20세 이상"),
        ("20세 이상 1인 가구 중 70세 이상 비율", None),
        ("70세 이상 1인 가구 중 여성 비율", None),
        ("노인 1인 가구 중 여성 비율", None),
        ("1인 가구 중 노인 비율", "20세 이상"),
        ("성인 1인 가구 중 여성 비율", "20세 이상"),
        ("노인과 청년 1인 가구 중 여성 비율", "20세 이상"),
        ("청년과 노인 1인 가구 중 여성 비율", "20세 이상"),
        ("65세 이상과 청년 1인 가구 중 여성 비율", "20세 이상"),
    ],
)
def test_age_limit_checks_the_population_side_of_a_ratio(question, limit) -> None:
    assert d.age_limit("성별·연령별 1인가구(20세 이상, 일반가구)", question) == limit


def test_named_age_in_a_list_does_not_widen_an_explicit_band() -> None:
    assert qa.resolved_ages("70세 이상 노인과 청년 1인 가구") == [[70, None], [19, 34]]


@pytest.mark.parametrize(("item", "minimum"), [("15살 넘은", 15), ("65세 초과", 66)])
def test_search_and_calculation_agree_on_age_cutoff(item, minimum) -> None:
    assert qa.extract_ages(item) == [[minimum, None]]
    understood = search_slots.empty_slots(item)
    understood["conditions"]["items"] = [item]
    table = {"name_n": "혼인상태별인구", "meta_n": "", "age_cov": (minimum, None)}
    assert search.check(table, understood)[f"item:{item}"]
    table["age_cov"] = (minimum + 1, None)
    assert not search.check(table, understood)[f"item:{item}"]


@pytest.mark.parametrize(
    ("table", "question"),
    [
        ("성별 통근 통학 인구(12세 이상)-시군구", "2020년 서울 통근 인구 몇 명"),
        ("출생자녀수별 기혼여성인구(15세 이상)-시도", "아이 둘 낳은 기혼여성 수"),
        ("1년 전 거주지별 인구(1세 이상)", "서울 1년 전 거주지별 인구 추이"),
    ],
)
def test_count_queries_allow_the_tables_survey_age(table, question) -> None:
    assert d.age_limit(table, question) is None


@pytest.mark.parametrize(
    ("question", "band"),
    [
        ("2010년에 15살 넘은 사람 중에 결혼 안 한 사람 비율 몇 퍼센트였어?", [15, None]),
        ("강북구에 100살 넘은 장애인 몇 명이야?", [100, None]),
        ("65세 넘는 70세 이하 1인 가구", [65, 70]),
        ("65세 초과", [66, None]),
        ("65세 초과 70세 이하 1인 가구", [66, 70]),
    ],
)
def test_colloquial_age_cutoff_includes_the_stated_age(question, band) -> None:
    assert qa.extract_ages(question) == [band]


def test_handoff_retries_others_when_the_exact_table_fails(computed) -> None:
    # 고른 표(모두 충족)가 계산되지 않으면 그 이유로 건너뛴 후보를 다시 계산한다 (11/20 D-020).
    # ✘ 후보는 그대로 건너뛴다
    computed["fail"] = {"A"}
    cands = [
        _cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"}),
        _cand("B", 2, checks={"year": False}),
        _cand("D", 3, checks={"year": True}),
    ]
    trace: dict = {}
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace))
    assert res["status"] == "ok" and trace["handoff"]["used"]["tbl_id"] == "D"
    assert sorted(computed["tried"]) == ["A", "D"]
    steps = {s["tbl_id"]: s for s in trace["dynamic"]}
    assert steps["B"]["skip"] == "통계표 찾기 조건 불충족 (year)"
    assert steps["D"]["retry"]


def test_handoff_falls_back_to_an_edition_with_the_asked_year(computed) -> None:
    edition = {"tbl_id": "A2020", "period": "2020-2024", "start_year": 2020, "end_year": 2024}
    cands = [
        _cand(
            "A",
            1,
            picked=True,
            checks={"year": False, "sex": True},
            years=(2010, 2019),
            editions=[edition],
        )
    ]
    trace: dict = {}
    res = asyncio.run(
        d.compute_from_handoff(Q1, BASE, _found(cands, status="partial_match"), trace)
    )
    assert res["status"] == "ok"
    assert trace["handoff"]["used"] == {"tbl_id": "A2020", "kind": "edition"}
    assert trace["dynamic"][0]["skip"] == "통계표 찾기 조건 불충족 (year)"


def test_handoff_revives_exclude_and_target_checks(computed) -> None:
    cands = [_cand("A", 1, picked=True, checks={"year": True})]
    trace: dict = {}
    u = _understanding(exclude=["귀화자"])
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands, u), trace)) is None
    assert "빼 달라는" in trace["dynamic"][0]["skip"]
    trace = {}
    u = _understanding(target="유학생")
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands, u), trace)) is None
    assert "대상 '유학생'" in trace["dynamic"][0]["skip"]
    # 표 이름에 대상이 있으면 통과
    named = [_cand("A", 1, picked=True, checks={"year": True}, tbl_nm="유학생 1인가구")]
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(named, u), {}))
    assert res["status"] == "ok"


def test_target_word_check_trusts_an_exact_review(computed) -> None:
    # 가 (11/18): 검토가 '모두 충족'으로 본 표는 대상 낱말이 글자로 없어도 계산한다
    u = _understanding(target="해외 유학생")
    exact = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"})]
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(exact, u), {}))["status"] == "ok"
    partial = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "partial"})]
    trace: dict = {}
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(partial, u), trace)) is None
    assert "대상 '해외 유학생'" in trace["dynamic"][0]["skip"]


def test_handoff_computes_with_search_reading(computed, monkeypatch) -> None:
    monkeypatch.setattr(
        indicators,
        "resolve_region",
        lambda name, *a, **k: {"status": "ok", "display": "부산광역시"},
    )
    cond = {**_understanding()["conditions"], "region": "부산", "year": 2022, "sex": "여"}
    age = {"expr": "노인", "lo": 65, "hi": None, "basis": "노인복지법"}
    u = _understanding(conditions=cond, age=age)
    parsed = {**BASE, "type": "other"}  # 수치 해석이 질문 종류를 못 정함
    q, diff = d.to_parsed(u, parsed)
    assert (q["regions"], q["year"], q["sex"], q["ages"]) == (
        ["부산광역시"],
        2022,
        "여",
        [[65, None]],
    )
    assert q["type"] == "value" and q["age_group"]["basis"] == "노인복지법"
    assert diff == {
        "regions": [["서울"], ["부산광역시"]],
        "year": [2020, 2022],
        "sex": ["계", "여"],
    }
    # 탐색 해석에 없는 칸은 수치 해석 그대로
    q2, _ = d.to_parsed(_understanding(), {**BASE, "regions": ["서울", "부산"]})
    assert q2["regions"] == ["서울", "부산"] and q2["sex"] == "계"


def test_school_going_is_commute_only_with_movement() -> None:
    # 이동을 말하면 "학교 가는 · 다니는"은 통학 (통근 + 통학을 함께 묻는 질문)
    assert {"통근", "통학"} <= {
        w for w in ("통근", "통학") if w in d.table_words("출근하거나 학교 가는 사람 수")
    }
    assert "통학" in d.table_words("버스 타고 학교에 다니는 학생 수")
    # 이동을 말하지 않으면 재학일 수 있어 그대로 둔다
    assert "통학" not in d.table_words("학교 다니는 학생 몇 명이야")
    # "고등학교 · 대학교"는 학교급 이름이라 바꾸지 않는다 (교육정도 칸과 맞아야 한다)
    words = d.table_words("지하철 타고 고등학교 다니는 학생 수")
    assert "고등학교" in words and "통학" not in words


def test_handoff_drops_guessed_year() -> None:
    # 질문 이해가 연도 없는 질문에 올해를 짐작해 넣었고(종류 other라 qa.correct가 안 지움)
    # 탐색 해석은 연도가 없다 → 지워서 표의 마지막 해로 계산한다
    no_year = _understanding(conditions={**_understanding()["conditions"], "year": None})
    q = {**BASE, "type": "other", "year": 2025}
    assert d.drop_guessed_year("버스로 출근하는 사람 수", no_year, q) and q["year"] is None
    # 질문에 연도를 말했으면 그대로 둔다 (숫자 연도 · 상대 연도)
    for said in ("2023년 버스로 출근하는 사람 수", "작년 버스로 출근하는 사람 수"):
        q = {**BASE, "type": "other", "year": 2023}
        assert not d.drop_guessed_year(said, no_year, q) and q["year"] == 2023
    # 탐색 해석에 연도가 있으면 to_parsed가 넣은 연도를 그대로 둔다
    q = {**BASE, "year": 2020}
    assert not d.drop_guessed_year("버스로 출근하는 사람 수", _understanding(), q)
    assert q["year"] == 2020


def test_handoff_notice_has_notes_and_age_basis() -> None:
    entry = {"kind": "picked", "notes": ["2020.11.1. 기준입니다."]}
    age = {"expr": "노인", "lo": 65, "hi": None, "basis": "노인복지법"}
    combine = {"out_of_scope": ["집값"]}
    text = d.handoff_notice(entry, {"age": age}, combine, "2020년 판 기준")
    assert text == (
        "2020년 판 기준. 2020.11.1. 기준입니다. "
        "'노인'은(는) 65세 이상으로 계산했습니다 (노인복지법). "
        "집값은(는) 인구 통계가 아니라 답하지 못했습니다"
    )


def test_handoff_missing_or_multi_uses_old_answer(monkeypatch) -> None:
    called = []

    async def old(question, parsed, found, trace):
        called.append(found.get("status"))
        return None

    multi = []

    async def several(question, parsed, found, trace):
        multi.append(len(found["handoff"]["targets"]))
        return None

    monkeypatch.setattr(d, "answer", old)
    monkeypatch.setattr(d, "multi_answer", several)
    asyncio.run(d.compute_from_handoff(Q1, BASE, {"status": "ok"}, {}))
    assert called == ["ok"]  # 꾸러미가 없으면 예전 방식
    two = _found([_cand("A", 1, picked=True)])
    two["handoff"]["targets"].append({"candidates": []})
    asyncio.run(d.compute_from_handoff(Q1, BASE, two, {}))
    assert multi == [2]  # 지표가 여럿이면 지표마다 계산해 합친다 (구현 5, multi_indicator.py)
    # 되묻기 · 범위 밖은 계산하지 않는다
    refused = _found([], status="clarify")
    refused["handoff"]["targets"] = []
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, refused, {})) is None


def test_handoff_exclude_ignores_region_names(computed, monkeypatch) -> None:
    # "서울이랑 경기 말고 시도별 …": 지역은 지역 칸이 고른다. 제외 검사로 표를 막지 않는다
    monkeypatch.setattr(
        indicators, "find_regions", lambda text: ["서울특별시"] if "서울" in text else []
    )
    cands = [_cand("A", 1, picked=True, checks={"year": True})]
    res = asyncio.run(
        d.compute_from_handoff(Q1, BASE, _found(cands, _understanding(exclude=["서울"])), {})
    )
    assert res["status"] == "ok"
    trace: dict = {}
    u = _understanding(exclude=["서울", "귀화자"])  # 지역이 아닌 말은 그대로 막는다
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands, u), trace)) is None
    assert "귀화자" in trace["dynamic"][0]["skip"] and "서울" not in trace["dynamic"][0]["skip"]


def test_handoff_offers_an_alternative_when_nothing_computes(computed, monkeypatch) -> None:
    # 고른 표(모두 충족)에서 칸을 못 고르고, 다른 후보는
    # 규칙으로 건너뜀 → 대안으로 계산해 LLM에 보임 (대안 기능은 기본 꺼짐이라 켜고 본다)
    from app.core.config import get_settings
    from app.services import alternative

    monkeypatch.setattr(get_settings(), "handoff_alternatives", True)
    computed["fail"] = {"A"}
    seen = {}

    async def judge(question, failures, alts):
        seen["alts"] = [a["entry"]["tbl_id"] for a in alts]
        return {
            "action": "offer",
            "offers": [{"id": "A1", "label": "다른 표 기준"}],
            "reason": "r",
        }, "gemma"

    monkeypatch.setattr(alternative, "judge", judge)
    # D는 다시 계산(고른 표가 안 될 때)에서도 한 번 못 고르고, 대안 계산에서야 계산된다
    calls = {"D": 0}
    inner = d._build

    async def build_d_later(tbl_id, *args, **kwargs):
        if tbl_id == "D":
            calls["D"] += 1
            if calls["D"] == 1:
                return None, "질문의 말과 맞는 칸이 없음"
        return await inner(tbl_id, *args, **kwargs)

    monkeypatch.setattr(d, "_build", build_d_later)
    cands = [
        _cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"}),
        _cand("D", 2, checks={"year": True}),
    ]
    trace: dict = {}
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace))
    assert seen["alts"] == ["D"]
    assert res["status"] == "clarify_question" and res["clarification"]["kind"] == "alternative"
    assert res["clarification"]["context"]["answers"][0]["status"] == "ok"
    assert any(s.get("alternative") for s in trace["dynamic"])  # 대안 계산도 기록에 남는다


# ---- 칸 고르기 LLM (cell_pick.py, 2026-11-18) ------------------------------------------


@pytest.fixture
def cell_llm(computed, fake_table, monkeypatch):
    """A 표는 규칙으로 칸을 못 고른다. 칸 고르기 LLM의 답은 state["reply"]"""
    from app.services import cell_pick

    computed["fail"] = {"A"}
    fake_table["tables"]["A"] = (ONE_PERSON, 1, "시군구")
    state = {"reply": None, "groups": None}

    async def judge(question, understanding, tbl_nm, groups):
        state["groups"] = [(g.no, g.name, [n for _, n in g.cells]) for g in groups]
        return state["reply"], "gemma"

    monkeypatch.setattr(cell_pick, "judge", judge)
    remembered = []

    async def remember(parsed, tbl_id, forced, res, words=()):
        remembered.append((tbl_id, forced, list(words)))

    monkeypatch.setattr(d, "_remember", remember)
    state["remembered"] = remembered
    return state


def test_cell_llm_computes_a_table_the_rule_could_not(cell_llm) -> None:
    cell_llm["reply"] = {
        "fits": "예",
        "picks": {"항목": "1"},
        "read_as": [{"질문": "혼자 사는 집", "표": "1인가구"}],
        "reason": "r",
    }
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"})]
    trace: dict = {}
    q = "2020년 서울 혼자 사는 집 수"
    res = asyncio.run(d.compute_from_handoff(q, BASE, _found(cands), trace))
    assert res["status"] == "ok"
    # 지역 · 연령 분류는 보내지 않는다. 항목은 성별 칸이 섞여 있어도 1인가구가 있어 보낸다
    assert cell_llm["groups"] == [(0, "항목", ["1인가구", "남자", "여자"])]
    steps = [s for s in trace["dynamic"] if s.get("tbl_id") == "A"]
    assert steps[0]["skip"] == "질문의 말과 맞는 칸이 없음"
    assert steps[0]["cell_llm"]["result"] == {"0": "T00"}
    assert steps[-1]["by"] == "cell_llm" and steps[-1]["forced"] == {0: "T00"}
    assert (
        "질문의 '혼자 사는 집'을(를) 표의 '1인가구'(으)로 보고 계산했습니다"
        in (res["parsed"]["dynamic_spec"]["note"])
    )
    # LLM이 고른 칸은 그 칸을 고른 근거 낱말과 함께 캐시에 남긴다 (다시 쓸 때 새 질문에 있는지 본다)
    assert cell_llm["remembered"] == [("A", {0: "T00"}, [["혼자 사는 집", "1인가구"]])]


def _judged(cell_llm, fake_table, monkeypatch, fail: set, reply_for) -> list:
    """규칙으로 못 고르는 표(fail)마다 칸 고르기 LLM을 부른 순서를 모은다.
    reply_for(표 이름) → 답"""
    from app.services import cell_pick

    for t in fail:
        fake_table["tables"][t] = (ONE_PERSON, 1, "시군구")
    called: list[str] = []

    async def judge(question, understanding, tbl_nm, groups):
        called.append(tbl_nm[0])
        return reply_for(tbl_nm[0]), "gemma"

    monkeypatch.setattr(cell_pick, "judge", judge)
    return called


YES = {"fits": "예", "reason": "r", "read_as": None, "picks": {"항목": "1"}}
NO = {"fits": "아니오", "reason": "r", "read_as": None, "picks": {}}


def test_cell_llm_is_not_called_below_a_table_the_rule_computed(
    cell_llm, computed, fake_table, monkeypatch
) -> None:
    # 규칙으로 계산된 첫 표(E, 3순위)보다 뒤의 표는 LLM으로 계산돼도 답이 되지 않아 부르지 않는다.
    # 그 앞(A · D)은 순위대로 부르고, 계산되면 멈춘다 (11/18)
    computed["fail"] = {"A", "D", "F"}
    called = _judged(cell_llm, fake_table, monkeypatch, {"A", "D", "F"}, lambda t: YES)
    cands = [
        _cand("A", 1, picked=True, checks={"year": True}, review={"match": "partial"}),
        _cand("D", 2, checks={"year": True}),
        _cand("E", 3, checks={"year": True}),
        _cand("F", 4, checks={"year": True}),
    ]
    trace: dict = {}
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace))
    assert res["status"] == "ok" and trace["handoff"]["used"]["tbl_id"] == "A"
    assert called == ["A"]  # A가 LLM으로 계산돼 D · F는 부르지 않음


def test_cell_llm_goes_down_in_rank_until_one_computes(
    cell_llm, computed, fake_table, monkeypatch
) -> None:
    computed["fail"] = {"A", "D", "F"}
    called = _judged(cell_llm, fake_table, monkeypatch, {"A", "D", "F"}, lambda t: NO)
    cands = [
        _cand("A", 1, picked=True, checks={"year": True}, review={"match": "partial"}),
        _cand("D", 2, checks={"year": True}),
        _cand("E", 3, checks={"year": True}),
        _cand("F", 4, checks={"year": True}),
    ]
    trace: dict = {}
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace))
    assert trace["handoff"]["used"]["tbl_id"] == "E" and res["status"] == "ok"
    assert called == ["A", "D"]  # 둘 다 '표에 없음' → 규칙으로 계산된 E. F는 부르지 않음


def test_cell_llm_calls_stop_at_the_cap(cell_llm, computed, fake_table, monkeypatch) -> None:
    fail = {"A", "D", "E", "F", "G"}
    computed["fail"] = fail
    called = _judged(cell_llm, fake_table, monkeypatch, fail, lambda t: NO)
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "partial"})]
    cands += [_cand(t, i, checks={"year": True}) for i, t in enumerate("DEFG", start=2)]
    trace: dict = {}
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace)) is None
    assert called == ["A", "D", "E"]  # 상한 MAX_CELL_LLM = 3
    skipped = [s["tbl_id"] for s in trace["dynamic"] if s.get("cell_llm_skip")]
    assert skipped == ["F", "G"]


def test_cell_llm_says_the_table_does_not_fit(cell_llm) -> None:
    cell_llm["reply"] = {"fits": "아니오", "picks": {}, "reason": "r"}
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"})]
    trace: dict = {}
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace)) is None
    assert trace["dynamic"][0]["cell_llm"]["result"] == "표에 없음"
    assert not any(s.get("by") == "cell_llm" for s in trace["dynamic"])


def test_cell_llm_unsure_asks_that_dimension(cell_llm) -> None:
    cell_llm["reply"] = {"fits": "예", "picks": {"항목": "정할 수 없음"}, "reason": "r"}
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"})]
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), {}))
    assert res["status"] == "clarify_question"
    assert res["clarification"]["kind"] == "table_cell"
    assert res["clarification"]["options"] == ["1인가구", "남자", "여자"]


@pytest.mark.parametrize("read_as", [None, [{"질문": "홀로 사는 가구", "표": "1인가구"}]])
def test_cell_llm_cells_count_for_the_condition_check(cell_llm, read_as) -> None:
    # 조건 '홀로 사는 가구'는 칸 이름에 글자로 없지만, 칸 고르기 LLM이 고른 칸은 조건까지 보고
    # 고른 것으로 본다 (11/18). LLM이 바꿔 읽은 말을 적지 않아도(read_as null) 마찬가지
    cell_llm["reply"] = {"fits": "예", "reason": "r", "read_as": read_as, "picks": {"항목": "1"}}
    u = _understanding()
    u["conditions"]["items"] = ["홀로 사는 가구"]
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"})]
    res = asyncio.run(
        d.compute_from_handoff("2020년 서울 홀로 사는 가구", BASE, _found(cands, u), {})
    )
    assert res["status"] == "ok"


def test_cell_llm_off(cell_llm, monkeypatch) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "handoff_cell_llm", False)
    cell_llm["reply"] = {"fits": "예", "picks": {"항목": "1"}, "reason": "r"}
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"})]
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), {})) is None
    assert cell_llm["groups"] is None


def test_cell_llm_fit_skips_the_target_word_check(cell_llm) -> None:
    # 나 (11/18): 칸 고르기 LLM이 "이 표가 질문의 값을
    # 담고 있다"고 답했으면 대상 낱말 검사를 하지 않는다
    cell_llm["reply"] = {"fits": "예", "reason": "r", "picks": {"항목": "1"}}
    u = _understanding(target="해외 유학생")
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "partial"})]
    trace: dict = {}
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands, u), trace))
    assert res["status"] == "ok"
    assert trace["dynamic"][-1]["by"] == "cell_llm"


def test_ratio_does_not_use_region_named_cell(monkeypatch) -> None:
    # 지역 분류가 둘인 표(현 거주지 · 통근통학지)에서 질문의 지역 이름이 통근통학지 칸에 걸려도
    # 그 칸 ÷ 전국을 질문의 비율로 내지 않는다 ("1인 가구 중 통근 비율" → 서울 ÷ 전국이 나왔다)
    monkeypatch.setattr(d.indicators, "find_regions", lambda t: [t] if t in ("서울특별시",) else [])
    items = d.Dim(0, "항목", {"T": "통근통학 인구"})
    place = d.Dim(2, "통근통학지별", {"00": "전국", "11": "서울특별시"})
    plan = d.Plan(pick={0: "T", 2: "11"}, matched={2: "11"})
    src = {"table": "X", "item_id": "T", "filters": {}}
    spec, why = d._ratio({"name": "비율"}, src, plan, [items, place], {"sex": "계"})
    assert spec is None and why == "비율의 분자·분모를 정하지 못함"


def test_top_region_of_with_one_target_is_not_answered(computed) -> None:
    # "A가 가장 많은 시도의 B"를 지표 하나로 읽으면 거르는 지표가 없어 전국 B가 나온다 → 표 안내
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"})]
    q = "2020년 1인 가구가 가장 많은 시도의 통근 인구는?"
    trace: dict = {}
    assert asyncio.run(d.compute_from_handoff(q, BASE, _found(cands), trace)) is None
    assert any("거르는 지표가 없음" in (s.get("skip") or "") for s in trace["dynamic"])


def test_cut_cell_llm_reply_without_picks_asks_instead_of_failing(cell_llm) -> None:
    # 칸 고르기 응답이 잘려 picks가 없으면 버리지 않고 그 분류를 되묻는다 (11/18)
    cell_llm["reply"] = {"fits": "예", "reason": "r", "_salvaged": True}
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"})]
    trace: dict = {}
    res = asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace))
    assert res["clarification"]["kind"] == "table_cell"
    assert trace["dynamic"][0]["cell_llm"]["result"] == "정할 수 없음 (응답 잘림)"


def test_condition_words_still_checked_without_cell_llm(computed) -> None:
    # 규칙으로 칸을 고른 표는 지금처럼 조건 낱말을 글자로 본다 (칸 고르기 LLM 때만 건너뜀)
    u = _understanding()
    u["conditions"]["items"] = ["홀로 사는 가구"]
    cands = [_cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"})]
    trace: dict = {}
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands, u), trace)) is None
    assert "홀로 사는 가구" in trace["dynamic"][0]["skip"]


def test_no_year_question_skips_old_tables(computed, monkeypatch) -> None:
    # 연도를 말하지 않은 질문은 최신 연도를 묻는 것: 최근 자료가 없는 옛 표(1970~1980)는 건너뛴다
    monkeypatch.setattr(d, "_recent_floor", lambda: 2020)
    u = _understanding()
    u["conditions"]["year"] = None
    cands = [
        _cand("OLD", 1, picked=True, checks={"region_level": True}, years=(1970, 1980)),
        _cand("NEW", 2, checks={"region_level": True}, years=(1990, 2025)),
    ]
    trace: dict = {}
    parsed = {k: v for k, v in BASE.items() if k != "year"}
    res = asyncio.run(d.compute_from_handoff("서울 1인 가구 수", parsed, _found(cands, u), trace))
    assert res["status"] == "ok" and trace["handoff"]["used"]["tbl_id"] == "NEW"
    assert "1980년까지인 표" in trace["dynamic"][0]["skip"]


# ---- 개발셋 2차: 표를 찾고도 계산하지 못하던 질문 ------------------------------------------------


def test_word_inside_cell_name() -> None:
    # 칸 이름이 질문에 그대로 없어도 질문 낱말이 칸 이름 안에 들면 그 칸
    names = {"T": "통근·통학 인구(12세 이상)", "A": "시내·좌석·마을 버스", "B": "전철 지하철",
             "C": "고속 시외 버스", "D": "통근·통학버스"}  # fmt: skip
    items = d.Dim(0, "항목", names)
    hits = d._text_hits("지하철이랑 시내버스 중에 뭘 더 많이 탔어", [items], set())
    assert sorted(hits[0]) == ["A", "B"]
    kids = d.Dim(0, "항목", {"0": "이혼건수-계", "1": "미성년자녀-없음", "2": "1명"})
    assert d._text_hits("이혼 부부 중 미성년 자녀 없는 경우", [kids], set()) == {0: ["1"]}
    # 낱말 하나가 칸 여럿에 걸리면 맞춘 것으로 보지 않는다 ("버스" → 버스 칸 셋)
    assert d._word_hits("버스 몇 명", items) == []


def test_more_is_a_comparison() -> None:
    assert d._multi_op("지하철이랑 시내버스 중에 뭘 더 많이 탔어") == "compare"
    assert d._multi_op("남자애가 더 많이 태어났어 여자애가 더 많이 태어났어?") == "compare"


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("이주배경인구_계", "이주배경인구"),  # 전에는 '계'만 떼어 "이주배경인구_ 수"
        ("다문화가구_가구원_계", "다문화가구 가구원"),
        ("아내의 연령별: 계", "아내의 연령별"),
        ("통학-계", "통학"),
        ("주택이외의 거처_계", "주택이외의 거처"),
        ("남자 계", "남자"),
        ("아시아주계", "아시아주"),  # 소계 이름 꼴 (대륙 · 내외국인 · 기타)
        ("외국인계", "외국인"),
        ("기타계", "기타"),
        ("중국계", "중국계"),  # 출신을 뜻하는 '계'는 낱말의 일부다 (전에는 "중국")
        ("일본계", "일본계"),
        ("중국_한국계", "중국 한국계"),
        ("연금 지급·증액 및 1세대 연금 자녀 승계", "연금 지급·증액 및 1세대 연금 자녀 승계"),
        ("합계", "합계"),  # 합계 칸 자체는 그대로 (이름에서는 TOTAL_NAMES가 뺀다)
    ],
)
def test_label_name_drops_total_tail_and_underscores(label: str, name: str) -> None:
    assert d._label_name(label) == name


def test_descent_cells_are_not_subtotals() -> None:
    # 순위 · 합의 세부 칸에서 "중국계"를 소계로 빼지 않는다. 소계(아시아주계, -계)는 뺀다
    codes = {"0": "계", "1": "아시아주계", "2": "중국", "3": "중국계", "4": "통학-계"}
    nat = d.Dim(2, "국적별", codes)
    assert d._leaf_codes(nat) == ["2", "3"]
    assert d.is_subtotal("아시아주계") and d.is_subtotal("일반가구-계")
    assert not d.is_subtotal("중국계") and not d.is_subtotal("자녀 승계")


@pytest.mark.parametrize(
    ("term", "name"),
    [
        ("이주배경인구_계 수", "이주배경인구 수"),
        ("이주배경인구_계", "이주배경인구"),
        ("다문화가구_가구원_계 수", "다문화가구 가구원 수"),
        ("이주배경인구 수", "이주배경인구 수"),
        ("중국_한국계 인구", "중국 한국계 인구"),  # 출신의 '계'는 그대로
    ],
)
def test_question_term_drops_raw_total_marks(term: str, name: str) -> None:
    # 질문에서 읽은 지표 표현을 이름으로 쓸 때도 원자료 합계 표시를 뗀다. 조회 칸은 그대로다
    item = d.Dim(0, "항목", {"T10": "이주배경인구_계", "T20": "다문화가구_가구원_계"})
    code = "T20" if "가구원" in term else "T10"
    if "중국" in term:
        item = d.Dim(0, "항목", {"T30": "중국_한국계", "T40": "일본계"})
        code = "T30"
    plan = d.Plan(pick={0: code}, matched={0: code})
    assert d._name({"term": term}, [item], plan, True, unit="명") == name
    assert plan.pick == {0: code}


MEMBERS = [  # 행안부 '읍면동별 다문화가구 현황': 가구원(사람)을 센 표
    d.Dim(0, "항목", {"T001": "다문화가구 현황"}),
    d.Dim(1, "행정구역(읍면동)별", {"00": "전국", "11": "서울특별시"}),
    d.Dim(2, "다문화가구별", {"A01": "합계", "A02": "한국인배우자", "A04": "자녀"}),
]
HOUSEHOLDS = [  # 통계청 '다문화가구 및 가구원': 가구를 센 칸
    d.Dim(0, "항목", {"T1": "다문화가구_계", "T2": "다문화가구원_계"}),
    d.Dim(1, "행정구역별(시군구)", {"00": "전국", "11": "서울특별시"}),
]


@pytest.mark.parametrize(
    ("tbl_id", "unit", "question", "ok"),
    [
        ("MEMBERS", "명", "2020년 서울 다문화가구 수는?", False),  # 가구 수인데 사람 수
        ("MEMBERS", "명", "2020년 서울 다문화가구 자녀 수는?", True),  # 사람을 묻는다
        ("MEMBERS", "명", "2020년 서울 다문화가구는 몇 명이야?", True),
        ("HOUSEHOLDS", "가구", "2020년 서울 다문화가구 수는?", True),
        # KOSIS 단위가 '명'이어도 칸 이름이 가구면 가구를 센 칸이다 (DT_1PQ2012 등)
        ("HOUSEHOLDS", "명", "2020년 서울 다문화가구 수는?", True),
    ],
)
def test_household_question_skips_person_count_cells(
    fake_table: dict, tbl_id: str, unit: str, question: str, ok: bool
) -> None:
    fake_table["tables"][tbl_id] = (MEMBERS if tbl_id == "MEMBERS" else HOUSEHOLDS, 1, "시도")
    fake_table["units"][tbl_id] = (unit, True)
    parsed = {**BASE, "indicator": "", "term": ""}
    spec, why = d.build(tbl_id, question, parsed)
    if ok:
        assert spec is not None, why
    else:
        assert spec is None and "사람 수" in str(why)


def test_name_from_underscored_total_cell() -> None:
    # "2023년 서울 이주배경인구 어때?": 질문 해석이 지표 표현을 비워 칸 이름으로 이름을 지었다
    item = d.Dim(0, "항목", {"T10": "이주배경인구_계", "T20": "내국인(귀화)_계"})
    plan = d.Plan(pick={0: "T10"}, matched={0: "T10"})
    assert d._name({}, [item], plan, True, unit="명") == "이주배경인구 수"
    unmatched = d.Plan(pick={0: "T10"}, matched={})  # 고른 칸 이름으로 짓는 경우도 같다
    assert d._name({}, [item], unmatched, True, unit="명") == "이주배경인구 수"


def test_name_drops_single_cell_item_and_unit_tail() -> None:
    # 항목이 하나뿐인 표의 항목 이름과 칸 끝의 단위 괄호는 이름에 쓰지 않는다 (11/20)
    item = d.Dim(0, "항목", {"T1": "출생사망혼인이혼"})
    kinds = d.Dim(2, "종류별", {"30": "이혼건수(건)", "31": "조이혼율(천명당)"})
    plan = d.Plan(pick={0: "T1", 2: "30"}, matched={0: "T1", 2: "30"})
    assert d._name({}, [item, kinds], plan, True, unit="건") == "이혼건수"
    # 질문의 지표 표현이 칸 이름을 담으면 그 표현
    assert d._name({"term": "이혼 건수"}, [item, kinds], plan, True, unit="건") == "이혼 건수"
    # 단위와 다른 괄호(조건)는 그대로 둔다
    women = d.Dim(0, "항목", {"A": "기혼여성인구(15세 이상)", "B": "미혼"})
    plan = d.Plan(pick={0: "A"}, matched={0: "A"})
    assert d._name({}, [women], plan, True, unit="명") == "기혼여성인구(15세 이상) 수"
    # 고른 칸이 하나뿐인 분류뿐이면 그 이름을 쓴다 (혼인 표의 항목 '혼인')
    only = d.Dim(0, "항목", {"T3": "혼인"})
    assert d._name({}, [only], d.Plan(pick={0: "T3"}, matched={0: "T3"}), True, unit="건") == (
        "혼인 수"
    )


REAL_UNIT = d._unit  # fake_table(자동)이 _unit을 바꾸기 전의 진짜 함수


def test_unit_follows_picked_cells_outside_item(monkeypatch) -> None:
    # 항목은 하나이고 단위가 종류별 칸마다 다른 표(인구동향): 고른 칸의 단위
    # (11/20 "11,798천명당")
    kinds = d.Dim(2, "종류별", {"30": "이혼건수(건)", "31": "조이혼율(천명당)"})
    item = d.Dim(0, "항목", {"T1": "출생사망혼인이혼"})
    dims = [item, d.Dim(1, "행정구역별", {"11": "서울"}), kinds]
    plan = d.Plan(pick={0: "T1", 1: "11", 2: "30"})
    assert d._unit_cells(plan, dims, 1) == {2: ["30"]}  # 항목 · 지역은 빼고
    plan.multi = (2, ["30", "31"], "compare")
    assert d._unit_cells(plan, dims, 1) == {2: ["30", "31"]}

    monkeypatch.setattr(lookup, "_codes", lambda t, n: {c: (f"{n}:{c}",) for c in dims[n].codes})
    monkeypatch.setattr(lookup, "_tbl_key", lambda t: 1)
    unit_of = {"2:30": "건", "2:31": "천명당"}
    seen = []

    def rows(sql, params):
        seen.append(params)
        picked = [unit_of[p] for p in params if p in unit_of]
        return [{"u": picked[0] if picked else "천명당", "whole": bool(picked)}]

    monkeypatch.setattr(d, "_rows", rows)
    monkeypatch.setattr(d, "_unit", REAL_UNIT)
    assert d._unit("T", "T1", {2: ["30"]}) == ("건", True)
    assert d._unit("T", "T1") == ("천명당", False)  # 항목만 보면 행이 많은 단위


def test_sexes_compared_within_item() -> None:
    items = d.Dim(0, "항목", {"0": "계", "1": "남자", "2": "여자"})
    plan, why = d.choose("작년에 남자애가 더 많이 태어났어 여자애가 더 많이 태어났어?",
                         {"sex": "계"}, [items], None)  # fmt: skip
    assert why is None and plan.multi == (0, ["1", "2"], "compare")


def test_husband_wife_dimension_defaults_to_husband() -> None:
    items = d.Dim(0, "항목", {"0": "이혼건수-계", "1": "미성년자녀-없음"})
    couple = d.Dim(1, "성별", {"1": "남편", "2": "아내"})
    plan, why = d.choose("이혼 부부 중 미성년 자녀 없는 경우", {"sex": "계"}, [items, couple], None)
    assert why is None and plan.pick[1] == "1"
    assert "부부 단위 건수" in plan.defaults[0]
    # 칸 이름 끝의 '-계'는 항목 전체 (비율의 분모)
    assert d._item_whole(items, "연령/미성년자녀수별 이혼") == "0"


def test_couple_counted_sex_dimension_uses_male_cell() -> None:
    # 성별이 남자 · 여자뿐이고 부부를 한 쪽씩 센 표(couple=True): 성별을 말하지 않으면 남자 칸
    items = d.Dim(0, "항목", {"T3": "혼인"})
    region = d.Dim(1, "시도별", {"00": "전국", "21": "부산광역시"})
    sex = d.Dim(2, "성별", {"1": "남자", "2": "여자"})
    q = "1978년 대구 혼인 건수"
    plan, why = d.choose(q, {"sex": "계"}, [items, region, sex], 1, couple=True)
    assert why is None and plan.pick[2] == "1" and plan.couple
    assert d.COUPLE_NOTE in plan.defaults[-1]
    # 부부 단위 표가 아니면(사람 수 표) 남자 · 여자를 따로 보여 준다
    plan, why = d.choose(q, {"sex": "계"}, [items, region, sex], 1)
    assert why is None and plan.split_sex == {"남": "남자", "여": "여자"} and not plan.couple
    # 연령을 물으면 남편 · 아내 나이가 달라 한 쪽 칸으로 답하지 않고 따로 보여 준다
    age = d.Dim(3, "연령(각세)별", {"000": "계", "230": "30세", "231": "31세", "232": "32세"})
    plan, why = d.choose(
        q, {"sex": "계", "ages": [[30, 34]]}, [items, region, sex, age], 1, couple=True
    )
    assert why is None and plan.split_sex == {"남": "남편", "여": "아내"} and plan.couple
    assert "남편 · 아내 따로" in plan.defaults[-1]
    # 성별을 말하면 그 칸. 부부 단위 표라는 표시는 그대로 (V2-035 "결혼한 남자 중 30대")
    plan, why = d.choose(q, {"sex": "여"}, [items, region, sex], 1, couple=True)
    assert why is None and plan.pick[2] == "2" and plan.couple
    plan, why = d.choose(q, {"sex": "남"}, [items, region, sex], 1, couple=True)
    assert why is None and plan.pick[2] == "1" and plan.couple
    # 사람 수 표에서 성별을 말한 경우는 부부 단위가 아니다
    plan, why = d.choose(q, {"sex": "남"}, [items, region, sex], 1)
    assert why is None and not plan.couple


def test_no_sex_total_split_builds_sexes_without_total(monkeypatch) -> None:
    # 성별 합계 칸이 없는 표에서 남녀를 따로 보여 줄 때: by_sex에 '계'가 없어야 qa.compute가
    # lookup.needs_split으로 남 · 여를 각각 계산한다. 이름표는 답의 "기혼자 수(남자)"
    items = d.Dim(0, "항목", {"1": "남자", "2": "여자"})
    region = d.Dim(1, "시도별", {"00": "전국", "11": "서울특별시"})
    kind = d.Dim(2, "초혼연령별", {"0": "15세이상자", "1": "기혼자"})
    monkeypatch.setattr(d, "schema", lambda t: ([items, region, kind], 1, "시도"))
    monkeypatch.setattr(d, "couple_counted", lambda t: False)
    monkeypatch.setattr(d.lookup, "_table", lambda t: {"tbl_nm": "시도/초혼연령/성별 기혼자"})
    monkeypatch.setattr(d, "_unit", lambda *a: ("명", None))
    parsed = {"type": "value", "indicator": "기혼자 수", "regions": ["서울"], "sex": "계"}
    spec, why = d.build("T_NO_TOTAL", "서울 기혼자 수", parsed)
    assert why is None
    by_sex = spec["sources"][0]["by_sex"]
    assert set(by_sex) == {"남", "여"} and by_sex["여"]["label"] == "여자"
    assert d.lookup.needs_split(spec, "계")
    # 비율은 남녀를 나눠 계산하지 않는다: 남자 칸 값이 답이 되지 않게 고르지 못한 것으로 둔다
    spec, why = d.build("T_NO_TOTAL", "서울 기혼자 비율", {**parsed, "indicator": "기혼자 비율"})
    assert spec is None and "칸을 고르지 못함" in str(why)


def test_couple_note_without_region_dimension() -> None:
    # 지역 구분이 없는 부부 단위 표 (다문화 유형별 이혼 DT_1BB0003): 지역 값 안내를 붙이지 않는다
    items = d.Dim(0, "항목", {"T00": "이혼건수"})
    sex = d.Dim(1, "성별", {"1": "남자", "2": "여자"})
    kind = d.Dim(2, "유형별", {"0": "계", "1": "출생기준 한국인", "2": "외국인"})
    plan, why = d.choose("다문화 이혼 건수", {"sex": "계"}, [items, sex, kind], None, couple=True)
    assert why is None and plan.pick[1] == "1" and plan.couple
    note = ", ".join(plan.defaults)
    assert d.COUPLE_NOTE_NATIONAL in note and "지역별" not in note


def test_couple_answer_drops_add_sexes_notice() -> None:
    # 부부 단위 표의 성별 칸이 곧 부부 건수: "남녀 값을 더해 보라"는 표 안내를 붙이면 두 번 세게
    # 된다 (V2-053). 성별을 말해 고른 칸도 같다 (V2-035)
    no_total = d.search_answer.NO_TOTAL_NOTE.rstrip(".")
    assert d.table_notice(no_total, True) is None
    assert d.table_notice(f"표 주석 A. {no_total}. 표 주석 B", True) == "표 주석 A. 표 주석 B"
    # 사람 수 표(부부 단위가 아님)에는 그대로 붙인다
    assert d.table_notice(no_total, False) == no_total
    assert d.table_notice(None, True) is None


def test_couple_counted_compares_national_sums(monkeypatch) -> None:
    dims = [
        d.Dim(0, "항목", {"T3": "혼인"}),
        d.Dim(1, "시도별", {"00": "전국", "21": "부산광역시"}),
        d.Dim(2, "성별", {"1": "남자", "2": "여자"}),
    ]
    monkeypatch.setattr(d, "schema", lambda t: (dims, 1, "시도"))
    monkeypatch.setattr(lookup, "_codes", lambda t, n: {c: (f"{n}:{c}",) for c in dims[n].codes})
    monkeypatch.setattr(lookup, "_tbl_key", lambda t: 1)
    sums = {"m": 283226.0, "f": 283226.0}
    seen = []

    def rows(sql, params):
        seen.append(params)
        return [dict(sums)]

    monkeypatch.setattr(d, "_rows", rows)
    d.couple_counted.cache_clear()
    assert d.couple_counted("T_COUPLE") is True
    assert "1:00" in seen[0]  # 전국 행만 본다
    sums["f"] = 280000.0  # 사람 수 표: 남 ≠ 여
    d.couple_counted.cache_clear()
    assert d.couple_counted("T_COUPLE") is False
    # 성별에 합계 칸이 있으면 해당하지 않는다 (값을 보지 않음)
    dims[2] = d.Dim(2, "성별", {"0": "계", "1": "남자", "2": "여자"})
    seen.clear()
    d.couple_counted.cache_clear()
    assert d.couple_counted("T_COUPLE") is False and not seen
    d.couple_counted.cache_clear()


def test_item_check_is_left_to_cell_check() -> None:
    # 통계표 찾기가 찾는 값을 ✘로 봐도 건너뛰지 않는다 (칸 반영 검사가 다시 본다)
    entry = {"kind": "picked", "checks": {"item:미성년 자녀 없는 경우": False, "year": True}}
    assert d.skip_reason(entry, False) is None
    assert d.skip_reason({**entry, "checks": {"year": False}}, False)


def test_husband_wife_are_sex_cells() -> None:
    # 혼인 · 이혼 표의 남편 · 아내는 성별 칸 ("이혼하는 남자 평균 나이" → 남편)
    items = d.Dim(0, "항목", {"1": "남편", "2": "아내"})
    plan, why = d.choose("이혼하는 남자 평균 나이", {"sex": "남"}, [items], None)
    assert why is None and plan.pick[0] == "1"


def test_pairs_question_needs_pair_unit(monkeypatch) -> None:
    # "쌍둥이 몇 쌍": 사람 수 칸("쌍태아 (명)")으로 답하지 않는다
    assert d.PAIRS_ASKED.search("지난해 태어난 쌍둥이 몇 쌍이야?")
    assert not d.PAIRS_ASKED.search("쌍둥이 몇 명")
    assert not d.PAIRS_ASKED.search("1980년 서울에서 이혼한 부부 몇 쌍이야?")  # 이혼 건수


def test_rank_dim_ignores_parentheses() -> None:
    nat = d.Dim(3, "국적(지역)별", {"0": "계", "1": "베트남", "2": "중국"})
    items = d.Dim(0, "항목", {"T": "등록외국인"})
    assert d._rank_dim("등록외국인 국적별로 많은 순서 세 나라", [items, nat], None, "") is nat


def test_per_average_divides_members_by_households() -> None:
    # "가구당 평균 가구원 수": 같은 묶음(일반가구)의 가구원 ÷ 가구, 비율이 아니라 평균
    assert d.PER_AVERAGE.search("가구당 평균 가구원 수 알려줘")
    items = d.Dim(0, "항목", {"a": "총가구_가구", "b": "총가구_가구원_계", "c": "일반가구_가구",
                              "e": "일반가구_가구원_계"})  # fmt: skip
    spec = d._per_average("T", [items, d.Dim(1, "행정구역별", {"00": "전국"})], 1, "시도")
    src = spec["sources"][0]
    assert (src["item_id"], src["den_item_id"], src["den_scale"]) == ("e", "c", 1)
    assert spec["name"] == "가구당 평균 가구원 수" and spec["unit"] == "명"
    assert d._per_average("T", [d.Dim(0, "항목", {"a": "가구"})], None, "시도") is None


def test_now_asked_words() -> None:
    assert d.NOW_ASKED.search("2015년 대비 지금 얼마나 늘었어")
    assert not d.NOW_ASKED.search("2015년 대비 2020년")


# ---- 같은 표에서 분자 찾기: 분자 말의 낱말이 모두 칸으로 설명될 때만 계산 ------------------------

NATION = [
    d.Dim(0, "항목", {"T1": "등록외국인"}),
    d.Dim(1, "행정구역별", {"00": "전국", "11": "서울특별시"}),
    d.Dim(2, "국적별", {"0": "계", "1": "베트남", "2": "중국", "3": "태국"}),
]


@pytest.fixture
def nation(computed, fake_table, monkeypatch):
    from app.services import ask_session

    fake_table["tables"]["NAT"] = (NATION, 1, "시도")
    fake_table["units"]["NAT"] = ("명", True)

    async def build_nat(tbl_id, org_id, question, parsed, step, forced=None, slots=None, **k):
        return d.build("NAT", question, parsed, forced, slots)

    async def answer(q, trace, question):
        result = {"status": "ok", "value": 7, "unit": "명", "year": 2024}
        return {"status": "ok", "result": result, "parsed": q}

    monkeypatch.setattr(d, "_build", build_nat)
    monkeypatch.setattr(qa, "answer", answer)
    spec = {"method": "lookup", "sources": [{"table": "NAT"}]}
    monkeypatch.setattr(indicators, "spec", lambda n: spec)
    q = {**BASE, "regions": ["전국"], "year": 2024, "indicator": None}

    def run(phrase, **k):
        trace: dict = {}
        got = asyncio.run(
            ask_session._same_table_side(phrase, "등록 외국인", q, "value", trace, **k)
        )
        return got, trace["cross_sides"][-1]

    return run


SAME_TABLE_KEYS = {"phrase", "same_table", "status", "leftover", "cells", "why", "unit", "options"}


@pytest.mark.parametrize("phrase", ["베트남 사람", "태국 국적자", "중국인", "베트남 출신"])
def test_same_table_counts_when_every_word_is_a_cell(nation, phrase) -> None:
    got, rec = nation(phrase)
    assert got and got["value"] == 7 and got["indicator"] == phrase
    assert rec["status"] == "ok" and rec["leftover"] == [] and rec["same_table"] == "NAT"
    assert set(rec) == SAME_TABLE_KEYS and rec["unit"] == "명"


@pytest.mark.parametrize(
    "phrase, left",
    [("태국 출신 결혼이민자", "결혼이민자"), ("중국 유학생", "유학생"), ("태국 근로자", "근로자")],
)
def test_same_table_blocks_word_without_cell(nation, phrase, left) -> None:
    # 국적 칸 하나만 맞고 대상을 좁히는 말이 남으면 그 나라 전체를 답하지 않는다 → 표 찾기로
    got, rec = nation(phrase)
    assert got is None
    assert rec["status"] == "uncovered" and rec["leftover"] == [left]
    assert rec["cells"] and rec["why"] == f"질문의 조건 '{left}'을 표의 칸에 반영할 수 없음"
    assert rec["options"]["국적별"] == ["계", "베트남", "중국", "태국"]
    assert "행정구역별" not in rec["options"]  # 지역 분류는 빼고
    assert set(rec) == SAME_TABLE_KEYS


def test_same_table_blocks_target_alone(nation) -> None:
    got, rec = nation("결혼이민자")
    assert got is None and rec["status"] in ("uncovered", "no_cell") and rec["leftover"]


def test_same_table_judges_only_given_phrase(nation) -> None:
    # 다시 쓴 말로 같은 표만 다시 부를 수 있다 (원래 질문의 낱말을 보태지 않는다)
    got, rec = nation("베트남", table="NAT")
    assert got and rec["status"] == "ok"


def test_leftover_words_drop_fillers_only() -> None:
    from app.services import ask_session

    assert ask_session._leftover_words("베트남 출신 결혼이민자") == ["베트남", "결혼이민자"]
    assert ask_session._leftover_words("베트남 국적자 수") == ["베트남"]
    # 이동 말이 있을 때만 '학교 가는'이 통학 (칸 고르기 공통의 table_words와 같다)
    assert ask_session._leftover_words("버스 타고 학교 가는 사람") == ["버스", "타고", "통학"]
    assert ask_session._leftover_words("고등학교 다니는 학생") == ["고등학교", "다니는", "학생"]


def test_same_table_unit_not_count_is_its_own_status(nation, monkeypatch) -> None:
    # 칸은 맞게 골라 계산했지만 값이 비율(%)이면 나누기에 쓸 수 없다. 칸 문제(no_cell)가 아니라
    # 단위 문제라 따로 적는다 (말을 다시 써도 같은 칸이 골라져 같은 이유로 실패한다)
    from app.services import ask_session

    async def percent(q, trace, question):
        result = {"status": "ok", "value": 12.5, "unit": "%", "year": 2024}
        return {"status": "ok", "result": result, "parsed": q}

    monkeypatch.setattr(qa, "answer", percent)
    got, rec = nation("베트남 사람")
    assert got is None
    assert rec["status"] == "not_count" and rec["status"] in ask_session.SAME_TABLE_STATUSES
    assert rec["cells"] == ["베트남"] and rec["leftover"] == [] and rec["unit"] == "%"
    assert rec["why"] == "건수 · 인원이 아닌 값 (%)" and set(rec) == SAME_TABLE_KEYS


# 표 이름에 있는 조건은 표가 이미 반영한 것 (통계표 찾기의 조건 확인과 같은 기준)
REMARRY = [d.Dim(0, "항목", {"H": "남편", "W": "아내"})]


@pytest.mark.parametrize(
    "items, words, why",
    [
        # 재혼 표: 재혼 칸 없이 아내 칸으로 답한다
        (["재혼"], ["시도별 평균재혼연령", None], None),
        # 표 이름에 없는 조건은 지금처럼 칸에서 찾는다 (넓은 값으로 답하지 않게)
        (["미혼"], ["시도별 평균재혼연령", None], "질문의 조건 '미혼'을 표의 칸에 반영할 수 없음"),
        # 표 이름이 없는 계산(넘기기 꾸러미 밖)은 지금 그대로
        (["재혼"], None, "질문의 조건 '재혼'을 표의 칸에 반영할 수 없음"),
    ],
)
def test_item_in_table_name_is_covered(items, words, why) -> None:
    slots = {"conditions": {"items": items}}
    if words is not None:
        slots["table_words"] = words
    plan = d.Plan(pick={0: "W"})
    assert d.uncovered(slots, plan, REMARRY, {"type": "trend"}) == why


# ---- 실제 테스트 오류 (11/23): 비율 이름 · 모집단 · 추계 문구 --------------------------------


def test_ratio_name_says_whole_part_and_ratio() -> None:
    # 값 정의 이름("… 수")을 비율에 그대로 쓰면 "… 수는 13.27%"가 됐다
    assert d._ratio_name("1인가구(20세이상 인구) 수", "70세 이상") == (
        "1인가구(20세이상 인구) 중 70세 이상 비율"
    )
    assert d._ratio_name("1인가구 여자 수", "여자") == "1인가구 중 여자 비율"
    assert d._ratio_name("1인 가구 중 70세 이상 비율", "70세 이상") == "1인 가구 중 70세 이상 비율"


def test_ratio_spec_is_named_as_ratio() -> None:
    spec = build("서울 1인 가구 중 여성 비율", indicator="2020년 서울", sex="여")
    assert spec["kind"] == "ratio" and spec["name"].endswith("비율")
    assert "수" not in spec["name"].split()


def test_handoff_keeps_priority_for_a_count_with_a_survey_age(computed, fake_table) -> None:
    fake_table["tables"]["DT_1PL1502"] = (
        [d.Dim(0, "항목", {"T00": "통근 인구"}), ONE_PERSON[1]],
        1,
        "시군구",
    )
    fake_table["units"]["DT_1PL1502"] = ("명", True)
    candidates = [
        _cand("A", 1, picked=True, checks={"year": True}, tbl_nm="통근 통학 인구(12세 이상)"),
        _cand("B", 2, checks={"year": True}, tbl_nm="통근 여부별 인구"),
    ]
    trace: dict = {}
    question = "2020년 서울 통근 인구 몇 명"
    parsed = {**BASE, "indicator": "통근 인구"}
    res = asyncio.run(d.compute_from_handoff(question, parsed, _found(candidates), trace))
    assert res["status"] == "ok" and sorted(computed["tried"]) == ["A", "B"]
    assert trace["handoff"]["used"]["tbl_id"] == "A"


def test_projection_answer_says_it_is_an_estimate(fake_table: dict) -> None:
    fake_table["projection"].add("DT_1PL1502")
    spec = build("2040년 서울 1인 가구 수", year=2040)
    assert spec["answer_note"] == d.PROJECTION_ANSWER
    # 첫 문장 바로 뒤에 붙는다 (qa._summary)
    result = {"status": "ok", "indicator": "1인 가구", "unit": "가구", "value": 1842002}
    result.update(year=2040, region="서울특별시", answer_note=spec["answer_note"])
    text = qa.summary(result)
    assert text.endswith("1,842,002가구입니다. " + d.PROJECTION_ANSWER)


def test_ratio_whole_target_drops_question_age() -> None:
    u = {"conditions": {"region": "서울"}, "age": {"expr": "70세 이상", "lo": 70, "hi": None}}
    assert d._target_understanding(u, {"span": "전체 1인 가구", "unsaid_age": True})["age"] is None
    kept = d._target_understanding(u, {"span": "70세 이상 1인 가구"})
    assert kept["age"]["lo"] == 70


@pytest.mark.parametrize(
    ("table", "question", "kind", "fits"),
    [
        ("통근 통학 인구(12세 이상)", "2020년 서울 통근 인구 몇 명", "value", True),
        ("통근 통학 인구(12세 이상)", "서울 통근 인구 추이", "trend", True),
        ("1인가구(65세 이상)", "1인 가구 중 70세 이상 비율", "value", False),
        ("1인가구(65세 이상)", "전체 1인 가구 수", "value", False),
        ("1인가구(65세 이상)", "전체 1인 가구 연령별 구성", "composition", False),
        ("1인가구(65세 이상)", "노인 1인 가구 중 여성 비율", "value", True),
        ("1인가구(65세 이상)", "65세 넘은 1인 가구 중 여성 비율", "value", True),
    ],
)
def test_cached_plan_on_age_limited_table_checks_question_scope(
    monkeypatch, table, question, kind, fits
) -> None:
    async def find(key):
        return [{"id": 1, "tbl_id": "DT_LIMITED", "forced": {}, "words": []}]

    used = []
    built = []

    async def build_(tbl_id, *args):
        built.append(tbl_id)
        return {"name": "지표", "kind": "count", "cells": []}, None

    async def answer_(*args):
        return {"status": "ok", "result": {"value": 1}}

    async def mark_used(id_):
        used.append(id_)

    monkeypatch.setattr(d.plan_cache, "key_of", lambda parsed: ("k", question))
    monkeypatch.setattr(d.plan_cache, "find", find)
    monkeypatch.setattr(d.plan_cache, "used", mark_used)
    monkeypatch.setattr(lookup, "_table", lambda t: {"tbl_nm": table})
    monkeypatch.setattr(d, "_build", build_)
    monkeypatch.setattr(qa, "answer", answer_)
    trace: dict = {}
    res = asyncio.run(d.cached_answer(question, {**BASE, "type": kind}, trace))
    if fits:
        assert res["status"] == "ok" and built == ["DT_LIMITED"] and used == [1]
        assert trace["plan_cache"] == {"hit": 1, "tbl_id": "DT_LIMITED"}
    else:
        assert res is None and built == [] and used == []
        assert "65세 이상만 집계" in trace["dynamic"][0]["skip"]


@pytest.mark.parametrize("flag", ["X", "-"])
def test_cached_answer_keeps_suppression_for_matching_table(monkeypatch, flag: str) -> None:
    primary, fallback = "DT_1PL1502", "DT_FALLBACK"

    async def find(key):
        return [{"id": 1, "tbl_id": primary}, {"id": 2, "tbl_id": fallback}]

    used = []
    built = []

    async def mark_used(plan_id):
        used.append(plan_id)

    async def build_(tbl_id, *args):
        built.append(tbl_id)
        return {
            "name": "1인 가구 수",
            "kind": "count",
            "cells": "1인가구",
            "sources": [{"table": tbl_id}],
        }, None

    async def answer_(parsed, *_args):
        tbl_id = parsed["dynamic_spec"]["sources"][0]["table"]
        if tbl_id == primary:
            lookup.EMPTY_FLAGS.get().append(flag)
            return {"status": "ok", "result": {"value": None}}
        return {"status": "ok", "result": {"value": 100}}

    monkeypatch.setattr(d.plan_cache, "key_of", lambda parsed: ("k", "1인 가구 수"))
    monkeypatch.setattr(d.plan_cache, "find", find)
    monkeypatch.setattr(d.plan_cache, "used", mark_used)
    monkeypatch.setattr(
        lookup,
        "_table",
        lambda tbl_id: {"tbl_nm": "성 및 연령별 1인가구", "org_nm": "KOSIS", "basis": ""},
    )
    monkeypatch.setattr(indicators, "kosis_url", lambda tbl_id: f"https://kosis.kr/{tbl_id}")
    monkeypatch.setattr(d, "_build", build_)
    monkeypatch.setattr(qa, "answer", answer_)

    trace: dict = {}
    res = asyncio.run(d.cached_answer("2020년 서울 1인 가구 수", BASE, trace))
    if flag == "X":
        assert res["status"] == "missing_data" and res["result"]["suppressed"]
        assert res["result"]["tbl_id"] == primary and "value" not in res["result"]
        assert built == [primary] and used == []
        assert res["trace"]["plan_cache"] == {"hit": 1, "tbl_id": primary}
    else:
        assert res["status"] == "ok" and res["result"]["value"] == 100
        assert built == [primary, fallback] and used == [2]
