import pytest

from app.services import cross_indicator, indicators, lookup


@pytest.mark.parametrize("regions", [["A", "B"], ["B", "A"]])
def test_cross_compare_uses_raw_order(regions):
    raw = {"A": 1.176, "B": 1.184}
    values = [{"region": r, "value": 1.18} for r in regions]
    result = cross_indicator._compare_facts(values, "%", raw=raw)
    assert result["highest"] == "B"
    assert result["lowest"] == "A"
    assert result["gap"] == 0.008
    assert result["ratio"] == {"a": "B", "b": "A", "value": 1.01}


@pytest.mark.parametrize("regions", [["A", "B"], ["B", "A"]])
@pytest.mark.parametrize("method", ["lookup", "computed"])
def test_compare_uses_raw_order_and_keeps_missing_values(monkeypatch, regions, method):
    regs = [{"display": r, "region_code": r} for r in [*regions, "C"]]
    raw = {"A": 1.176, "B": 1.184, "C": None}
    spec = {"name": "비율", "kind": "ratio", "unit": "%", "formula": "비율"}
    if method == "lookup":
        monkeypatch.setattr(lookup, "_prepare", lambda *a: (regs, {}, None, "", []))
        monkeypatch.setattr(
            lookup, "_fetch", lambda *a: {(2025, r): value for r, value in raw.items()}
        )
        monkeypatch.setattr(lookup, "_base", lambda *a, **kwargs: kwargs)
        result = lookup.compare(spec, regions, 2025)
    else:
        spec["calc"] = lambda comp: comp["value"]
        monkeypatch.setattr(indicators, "_prepare", lambda *a: ("비율", spec, regs, None))
        monkeypatch.setattr(indicators, "_check", lambda *a: None)
        monkeypatch.setattr(indicators, "_base", lambda *a, **kwargs: kwargs)
        monkeypatch.setattr(
            indicators,
            "_components",
            lambda *a: {(2025, r): {"value": value} for r, value in raw.items()},
        )
        monkeypatch.setattr(indicators, "_mark_monthly", lambda result, *a: result)
        result = indicators.compare("비율", regions, 2025)
    assert result["highest"] == "B"
    assert result["lowest"] == "A"
    assert result["gap"] > 0
    assert result["values"] == [
        {"region": "B", "value": 1.18},
        {"region": "A", "value": 1.18},
        {"region": "C", "value": None},
    ]


def test_rank_uses_unrounded_values() -> None:
    raw = {"A": 1.1761, "B": 1.1840, "C": 0.9}
    vals = [{"region": r, "value": cross_indicator._round(v)} for r, v in raw.items()]
    assert vals[0]["value"] == vals[1]["value"] == 1.18
    high = cross_indicator._rank_facts(vals, {"order": "높은 순"}, {}, raw=raw)
    assert [(x["rank"], x["region"]) for x in high["ranking"]] == [(1, "B"), (2, "A"), (3, "C")]
    assert high["ranking"][0]["value"] == 1.18
    low = cross_indicator._rank_facts(vals, {"order": "낮은 순"}, {}, raw=raw)
    assert [x["region"] for x in low["ranking"]] == ["C", "A", "B"]
