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
    stay = d.Dim(3, "거주기간별", {"0": "계", "1": "1년미만", "2": "1~5년"})
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
    computed["fail"] = {"A"}
    cands = [
        _cand("A", 1, picked=True, checks={"year": True}, review={"match": "exact"}),
        _cand("D", 2, checks={"year": True}),
    ]
    trace: dict = {}
    assert asyncio.run(d.compute_from_handoff(Q1, BASE, _found(cands), trace)) is None
    assert trace["dynamic"][1]["skip"] == "검토가 고른 표를 '모두 충족'으로 봄"


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

    monkeypatch.setattr(d, "answer", old)
    asyncio.run(d.compute_from_handoff(Q1, BASE, {"status": "ok"}, {}))
    two = _found([_cand("A", 1, picked=True)])
    two["handoff"]["targets"].append({"candidates": []})
    asyncio.run(d.compute_from_handoff(Q1, BASE, two, {}))
    assert called == ["ok", "ok"]
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
    # 규칙으로 건너뜀 → 대안으로 계산해 LLM에 보임
    from app.services import alternative

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

    async def remember(parsed, tbl_id, forced, res):
        remembered.append((tbl_id, forced))

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
    assert cell_llm["remembered"] == [("A", {0: "T00"})]  # LLM이 고른 칸은 캐시에 함께


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


def test_cell_llm_reading_counts_for_the_condition_check(cell_llm) -> None:
    # 조건 '홀로 사는 가구'는 칸 이름에 글자로 없지만, LLM이 그 말을 1인가구로 읽었으면 반영된 것
    cell_llm["reply"] = {
        "fits": "예",
        "picks": {"항목": "1"},
        "read_as": [{"질문": "홀로 사는 가구", "표": "1인가구"}],
        "reason": "r",
    }
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
