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
    return {"tables": tables, "units": units, "projection": projection}


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


def test_refuses_two_cells_in_one_dimension(fake_table: dict) -> None:
    stay = d.Dim(3, "거주기간별", {"0": "계", "1": "1년미만", "2": "1~5년"})
    fake_table["tables"]["DT_1PL1502"] = ([*ONE_PERSON, stay], 1, "시군구")
    spec, why = d.build("DT_1PL1502", "서울 1년미만과 1~5년 1인 가구", BASE)
    assert spec is None and why == "거주기간별에서 칸이 여럿 맞음"


def test_refuses_dimension_without_total(fake_table: dict) -> None:
    dims = [*ONE_PERSON, d.Dim(3, "거주기간별", {"1": "1년 미만", "2": "1~5년"})]
    fake_table["tables"]["DT_1PL1502"] = (dims, 1, "시군구")
    spec, why = d.build("DT_1PL1502", "서울 1인 가구 수", BASE)
    assert spec is None and why == "거주기간별에서 칸을 고르지 못함"


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


def test_answer_skips_clarify_and_tries_candidates(monkeypatch) -> None:
    assert asyncio.run(d.answer("질문", BASE, {"status": "clarify"}, {})) is None
    tried = []

    async def fake_build(tbl_id, org_id, question, parsed, step):
        tried.append(tbl_id)
        return (None, "질문의 말과 맞는 칸이 없음") if tbl_id == "A" else (build(question), None)

    async def fake_answer(q, trace, question):
        return {"status": "ok", "result": {"status": "ok", "value": 1390701}, "parsed": q}

    monkeypatch.setattr(d, "_build", fake_build)
    monkeypatch.setattr(qa, "answer", fake_answer)
    found = {"status": "ok", "recommendations": [{"tbl_id": "A"}, {"tbl_id": "DT_1PL1502"}]}
    trace: dict = {}
    res = asyncio.run(d.answer("2020년 서울 1인 가구 수", BASE, found, trace))
    assert tried == ["A", "DT_1PL1502"] and res["parsed"]["dynamic_spec"]["name"] == "1인 가구 수"
    assert trace["dynamic"][0]["skip"] == "질문의 말과 맞는 칸이 없음"


def test_nothing_fits_returns_none(monkeypatch) -> None:
    async def fake_build(tbl_id, org_id, question, parsed, step):
        return None, "질문의 말과 맞는 칸이 없음"

    monkeypatch.setattr(d, "_build", fake_build)
    found = {"status": "ok", "recommendations": [{"tbl_id": "A"}, {"tbl_id": "B"}]}
    assert asyncio.run(d.answer("서울 통근 인구", BASE, found, {})) is None


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


def test_answer_skips_table_without_values(monkeypatch) -> None:
    async def fake_build(tbl_id, org_id, question, parsed, step):
        return build(question), None

    async def fake_answer(q, trace, question):
        value = None if len(trace["dynamic"]) == 1 else 7  # 첫 표는 값이 비공개
        return {"status": "ok", "result": {"status": "ok", "value": value}, "parsed": q}

    monkeypatch.setattr(d, "_build", fake_build)
    monkeypatch.setattr(qa, "answer", fake_answer)
    found = {"status": "ok", "recommendations": [{"tbl_id": "A"}, {"tbl_id": "B"}]}
    trace: dict = {}
    res = asyncio.run(d.answer("2020년 서울 1인 가구 수", BASE, found, trace))
    assert res["result"]["value"] == 7
    assert trace["dynamic"][0]["skip"] == "값이 없음 (비공개·결측)"
