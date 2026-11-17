"""동적 지표 계산 1단계 (dynamic_indicator.py): 표의 칸으로 지표 정의 만들기

DB 없이: 1인가구 표(DT_1PL1502)의 칸 목록을 그대로 흉내 낸다.
  항목 T00 1인가구 / T10 남자 / T20 여자, c1 행정구역, c2 연령별 (000 합계, 024 20세미만 …)
"""

import asyncio

import pytest

from app.services import dynamic_indicator as d
from app.services import indicators, lookup, qa

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
    monkeypatch.setattr(d, "_unit", lambda tbl_id, item: units[tbl_id])
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


def test_lenient_uses_ambiguous_candidates(monkeypatch) -> None:
    # 옛 연도 찾기(lenient)는 '비슷한 표 여럿'의 후보로도 계산해 본다. 일반 질문은 되묻기 그대로
    tried = []

    async def fake_build(tbl_id, org_id, question, parsed, step, forced=None, slots=None):
        tried.append(tbl_id)
        return build(question), None

    async def fake_answer(q, trace, question):
        return {"status": "ok", "result": {"status": "ok", "value": 7935}, "parsed": q}

    monkeypatch.setattr(d, "_build", fake_build)
    monkeypatch.setattr(qa, "answer", fake_answer)
    families = [{"tbl_id": "DT_OLD", "tbl_nm": "옛 인구동태"}, {"tbl_id": "DT_OLD2"}]
    found = {"status": "ambiguous", "clarify": {"level": "family", "families": families}}
    assert asyncio.run(d.answer("1980년 서울 1인 가구 수", BASE, found, {})) is None
    res = asyncio.run(d.answer("1980년 서울 1인 가구 수", BASE, found, {}, lenient=True))
    assert res["status"] == "ok" and tried == ["DT_OLD"]


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
