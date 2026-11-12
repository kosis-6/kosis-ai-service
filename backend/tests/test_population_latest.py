"""연간 자료 다음 해의 인구: 같은 표(DT_1B04006)의 최신 달 값으로 답한다 (장래추계 대신)"""

import pytest

from app.services import indicators, population_latest, qa

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

    def boom(code, item):
        calls.append(code)
        raise RuntimeError("40: 호출 초과 (https://x?apiKey=SECRET)")

    monkeypatch.undo()  # conftest의 no_latest_month를 걷어 내고 진짜 components를 쓴다
    monkeypatch.setattr(population_latest, "_fetch_month", boom)
    monkeypatch.setattr(population_latest, "_cache", {})
    assert population_latest.components("11") is None
    assert population_latest.components("11") is None
    assert calls == ["11"]  # 실패한 뒤 잠시는 다시 부르지 않는다


@pytest.fixture
def october(monkeypatch: pytest.MonkeyPatch) -> dict:
    hi = indicators._data_years()[1]
    comp = population_latest._parse([{**r, "PRD_DE": f"{hi + 1}10"} for r in ROWS])
    monkeypatch.setattr(population_latest, "components", lambda code, sex="계": comp)
    return comp


def test_next_year_is_answered_with_latest_month(october: dict) -> None:
    year = october["year"]
    r = indicators.value("총인구", "서울", year)
    assert r["status"] == "ok" and r["value"] == 9274392
    assert r["tbl_id"] == "DT_1B04006" and "DT_1BPB002" not in r["url"]  # 추계표가 아니다
    assert r["as_of"] == f"{year}년 10월 말" and "월별" in r["basis"]
    assert qa.summary(r).startswith(f"{year}년 10월 말 서울특별시의 총인구는 9,274,392명")


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
