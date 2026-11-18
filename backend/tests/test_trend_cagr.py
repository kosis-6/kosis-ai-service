"""추이의 연평균 증감률 (qa._add_trend_facts → change["cagr"], qa._body의 전체 변화 줄. 11/18)"""

from app.services import qa


def trend(values, diff_unit="명", rate=True, start=2020):
    series = [{"year": start + i, "value": v} for i, v in enumerate(values)]
    first, last = values[0], values[-1]
    change = {
        "from_year": start,
        "to_year": start + len(values) - 1,
        "diff": last - first,
        "diff_unit": diff_unit,
    }
    if rate:
        change["rate"] = round((last - first) / first * 100, 2)
        change["rate_unit"] = "%"
    return {"region": "가시", "indicator": "지표", "unit": diff_unit, "sex": "계",
            "series": series, "change": change}  # fmt: skip


def test_cagr_is_compound_yearly_rate() -> None:
    r = trend([1000, 1100, 1210, 1331])  # 해마다 10%씩
    qa._add_trend_facts(r)
    assert r["change"]["cagr"] == 10.0
    assert qa._body(r)[-1] == "- 2020년 → 2023년 전체 변화: +331명, +33.1%, 연평균 +10.0%"


def test_cagr_when_decreasing() -> None:
    r = trend([200, 150, 100])
    qa._add_trend_facts(r)
    assert r["change"]["cagr"] == -29.3


def test_no_cagr_for_ratio_points_or_without_rate() -> None:
    ratio = trend([2.0, 3.0], diff_unit="%p")  # 비율 지표는 %p와 헷갈려 뺀다
    qa._add_trend_facts(ratio)
    assert "cagr" not in ratio["change"]
    norate = trend([5, 7], rate=False)
    qa._add_trend_facts(norate)
    assert "cagr" not in norate["change"]


def test_no_cagr_when_a_value_is_not_positive() -> None:
    r = trend([-50, 30, 80])  # 순이동처럼 음수에서 시작
    qa._add_trend_facts(r)
    assert "cagr" not in r["change"]
