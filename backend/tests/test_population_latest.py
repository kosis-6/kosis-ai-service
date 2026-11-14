"""연간 자료 다음 해의 인구: 같은 표(DT_1B04006)의 최신 달 값으로 답한다 (장래추계 대신)"""

import pytest

from app.services import charts, indicators, population_latest, qa

ROWS = [
    {"PRD_DE": "202610", "C2_NM": "계", "DT": "9274392"},
    {"PRD_DE": "202610", "C2_NM": "0세", "DT": "49818"},
    {"PRD_DE": "202610", "C2_NM": "14세", "DT": "50000"},
    {"PRD_DE": "202610", "C2_NM": "15세", "DT": "60000"},
    {"PRD_DE": "202610", "C2_NM": "65세", "DT": "70000"},
    {"PRD_DE": "202610", "C2_NM": "100세 이상", "DT": "1504"},
]


def test_parse_latest_month() -> None:
    comp = population_latest._parse(ROWS)
    assert (comp["year"], comp["month"], comp["total"]) == (2026, 10, 9274392)
    assert comp["young"] == 49818 + 50000 and comp["working"] == 60000
    assert comp["old"] == 70000 + 1504  # "100세 이상"도 65세 이상에 넣는다
    assert population_latest.band(comp, 20, 29) is None
    assert population_latest._parse([]) is None


def test_fetch_failure_is_cached_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def boom(code, item, period=None):
        calls.append(code)
        raise RuntimeError("40: 호출 초과 (https://x?apiKey=SECRET)")

    monkeypatch.undo()  # conftest의 no_latest_month를 걷어 내고 진짜 components를 쓴다
    monkeypatch.setattr(population_latest, "_fetch_month", boom)
    monkeypatch.setattr(population_latest, "_cache", {})
    assert population_latest.components("11") is None
    assert population_latest.components("11") is None
    assert calls == ["11"]  # 실패한 뒤 잠시는 다시 부르지 않는다


def test_many_regions_are_fetched_in_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def fetch(codes, item, period=None):
        calls.append(codes)
        return [{**r, "C1": c} for c in codes.split("+") if c != "99" for r in ROWS]

    monkeypatch.undo()
    monkeypatch.setattr(population_latest, "_fetch_month", fetch)
    monkeypatch.setattr(population_latest, "_cache", {})
    monkeypatch.setattr(population_latest, "CHUNK", 2)
    got = population_latest.components_many(["11", "26", "27", "99"])
    assert calls == ["11+26", "27+99"] and set(got) == {"11", "26", "27"}  # 99: 자료 없음
    population_latest.components_many(["11", "26", "99"])
    assert len(calls) == 2  # 받은 지역(자료 없음 포함)은 다시 부르지 않는다


@pytest.fixture
def october(monkeypatch: pytest.MonkeyPatch) -> dict:
    hi = indicators._data_years()[1]
    comp = population_latest._parse([{**r, "PRD_DE": f"{hi + 1}10"} for r in ROWS])
    monkeypatch.setattr(
        population_latest,
        "components_many",
        lambda codes, sex="계", period=None: dict.fromkeys(codes, comp),
    )
    return comp


def test_next_year_is_answered_with_latest_month(october: dict) -> None:
    year = october["year"]
    r = indicators.value("총인구", "서울", year)
    assert r["status"] == "ok" and r["value"] == 9274392
    assert r["tbl_id"] == "DT_1B04006" and "DT_1BPB002" not in r["url"]  # 추계표가 아니다
    assert r["as_of"] == f"{year}년 10월 말" and "월별" in r["basis"]
    assert qa.summary(r).startswith(f"{year}년 10월 말 서울특별시의 총인구는 9,274,392명")


def test_latest_month_keeps_region_validity_check(october: dict) -> None:
    # 최신 달로 답하는 해까지 물어도, 그 지역이 없던 해부터 물으면 안내한다
    # (2023년에 대구로 편입된 군위군을 2020년부터 물음)
    year = october["year"]
    r = indicators.trend("총인구", "대구 군위군", 2020, year)
    assert r["status"] == "region_not_in_year" and "2023년부터" in r["reason"]


def test_without_latest_month_projection_is_kept() -> None:
    year = indicators._data_years()[1] + 1
    r = indicators.value("총인구", "서울", year)  # conftest: 최신 달을 받지 못함
    assert r["status"] == "out_of_range"


def test_recent_question_uses_latest_month(october: dict) -> None:
    q = qa.correct(
        "최근 서울 인구수 알려줘",
        {"type": "value", "indicator": "총인구", "regions": ["서울"], "year": 2025, "sex": "계"},
    )
    assert q["asked"]["latest"] is True
    r = qa.compute(q)
    assert r["year"] == october["year"] and r["value"] == 9274392
    assert "latest" not in qa.correct("최근 5년 서울 인구", {"type": "trend"}).get("asked", {})
    assert "latest" not in qa.correct("2024년 서울 인구", {"type": "value"}).get("asked", {})


def test_rank_with_latest_month(october: dict) -> None:
    r = indicators.rank("총인구", october["year"], "시도", top=3)
    assert r["status"] == "ok" and r["as_of"] == f"{october['year']}년 10월 말"
    # 최신 달 순위는 그해 지역으로: 2026년은 광주·전남이 전남광주통합특별시(12)로 합쳐져 16곳
    # (예전에는 연간 마지막 해의 지역 29·46으로 찾아 최신 달 자료에 없어 조용히 빠졌다)
    assert r["total_regions"] == 16 and r["ranking"][0]["value"] == 9274392
    every = indicators.rank("총인구", october["year"], "시도")["ranking"]
    assert "전남광주통합특별시" in [i["region"] for i in every]
    assert qa.summary(r).startswith(f"{october['year']}년 10월 말")
    assert charts.build(r)["title"].startswith(f"{october['year']}년 10월 말 총인구")


def test_latest_month_rank_notes_regions_it_could_not_get(monkeypatch: pytest.MonkeyPatch) -> None:
    # 최신 달 자료를 일부 지역만 받으면 빠진 지역을 알린다 (전체 순위처럼 보이지 않게)
    hi = indicators._data_years()[1]
    comp = population_latest._parse([{**r, "PRD_DE": f"{hi + 1}10"} for r in ROWS])
    monkeypatch.setattr(
        population_latest,
        "components_many",
        lambda codes, sex="계", period=None: {c: comp for c in codes if c != "11"},
    )
    r = indicators.rank("총인구", hi + 1, "시도")
    assert r["status"] == "ok" and "서울특별시" not in [i["region"] for i in r["ranking"]]
    assert any("순위에서 빠졌습니다" in n and "서울특별시" in n for n in r["notes"])


def test_composition_with_latest_month(october: dict) -> None:
    r = indicators.composition("서울", october["year"], "age")
    assert r["status"] == "ok" and r["as_of"] == f"{october['year']}년 10월 말"
    assert [p["count"] for p in r["parts"]] == [99818, 60000, 71504]


def test_recent_rank_and_composition_use_latest_month(october: dict) -> None:
    rank = qa.correct(
        "현재 시도별 인구 순위", {"type": "rank", "indicator": "총인구", "year": 2025, "sex": "계"}
    )
    assert qa.compute(rank)["year"] == october["year"]
    comp = qa.correct(
        "현재 서울 인구의 연령대별 구성비",
        {"type": "composition", "indicator": "총인구", "regions": ["서울"], "year": 2025},
    )
    assert qa.compute(comp)["year"] == october["year"]


def test_year_already_passed_uses_december(monkeypatch: pytest.MonkeyPatch) -> None:
    """최신 달이 다음 해로 넘어갔으면 (2027년 1월) 연간 자료에 없는 해(2026년)는 그해 12월 값"""
    hi = indicators._data_years()[1]
    jan = population_latest._parse([{**r, "PRD_DE": f"{hi + 2}01"} for r in ROWS])
    dec = population_latest._parse([{**r, "PRD_DE": f"{hi + 1}12"} for r in ROWS])
    asked = []

    def many(codes, sex="계", period=None):
        asked.append(period)
        return dict.fromkeys(codes, dec if period else jan)

    monkeypatch.setattr(population_latest, "components_many", many)
    r = indicators.value("총인구", "서울", hi + 1)
    assert r["status"] == "ok" and r["as_of"] == f"{hi + 1}년 12월 말"
    assert f"{hi + 1}12" in asked
