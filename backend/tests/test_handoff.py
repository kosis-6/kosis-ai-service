import asyncio

import pytest

from app.services import search_log, table_guide
from app.services import slots as sl
from tests.test_agent_graph import TARGET_Q, TWO, TWO_PLAN, world  # noqa: F401 (fixture)


@pytest.fixture
def quiet_log(monkeypatch):
    async def write(*args, **kwargs):
        return None

    monkeypatch.setattr(search_log, "write", write)


def _find(question: str, session: str = "S_h") -> dict:
    return asyncio.run(table_guide.find_tables(question, session, None, 1))


def test_one_indicator_question_hands_off_up_to_five_candidates(world, quiet_log) -> None:  # noqa: F811
    found = _find("시도별 혼인")
    h = found["handoff"]
    assert (h["version"], h["status"], h["question"]) == (1, "ok", "시도별 혼인")
    assert len(h["targets"]) == 1
    target = h["targets"][0]
    cands = target["candidates"]
    assert 1 <= len(cands) <= table_guide.MAX_CANDIDATES
    first = cands[0]
    assert first["tbl_id"] == found["recommendations"][0]["tbl_id"] == "M1"
    assert (first["picked"], first["source"], first["rank"]) == (True, "whole", 1)
    assert [c["rank"] for c in cands] == list(range(1, len(cands) + 1))
    assert not any(c["picked"] for c in cands[1:])
    assert len({c["family_id"] for c in cands}) == len(cands)
    for c in cands:
        assert isinstance(c["checks"], dict) and isinstance(c["editions"], list)
    assert h["combine"] == {
        "op": "none",
        "note": None,
        "num": None,
        "den": None,
        "out_of_scope": [],
    }


def test_existing_response_fields_are_unchanged(world, quiet_log, monkeypatch) -> None:  # noqa: F811
    with_handoff = _find("시도별 혼인", "S_a")

    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(table_guide, "build_handoff", broken)
    without = _find("시도별 혼인", "S_b")
    assert without["handoff"] is None
    for key in ("status", "answer", "parsed", "recommendations", "together", "notice"):
        assert with_handoff[key] == without[key]


def test_several_indicators_get_one_target_each(world, quiet_log) -> None:  # noqa: F811
    world.plans[TWO] = TWO_PLAN
    h = _find(TWO)["handoff"]
    assert [t["span"] for t in h["targets"]] == ["혼인", "외국인 근로자 현황"]
    assert [t["candidates"][0]["tbl_id"] for t in h["targets"]] == ["M1", "W1"]
    assert all(c["source"] == "indicator" for t in h["targets"] for c in t["candidates"])
    assert all(c["review"] is None for t in h["targets"] for c in t["candidates"])
    assert h["combine"]["op"] == "compare" and h["combine"]["out_of_scope"] == ["집값"]


def test_refused_or_asking_results_hand_off_no_targets(world, quiet_log) -> None:  # noqa: F811
    refused = _find("오늘 점심 메뉴", "S_r")["handoff"]
    assert refused["targets"] == [] and refused["status"] in ("out_of_scope", "not_statistics")
    asking = _find(TARGET_Q, "S_c")
    assert asking["status"] == "clarify"
    assert asking["handoff"]["targets"] == []
    assert asking["handoff"]["clarify"] == asking["clarify"]


def test_understanding_carries_what_the_response_used_to_drop() -> None:
    slots = sl.empty_slots("서울 노인 인구, 귀화한 사람 말고")
    slots["conditions"].update(region="서울특별시", year=2025, sex="여")
    slots.update(
        exclude=["귀화한"],
        age={"expr": "노인", "lo": 65, "hi": None, "basis": "노인복지법"},
        projection=False,
        search_query="고령 인구",
        dropped_conditions=["거주형태"],
    )
    u = table_guide._understanding(slots)
    assert u["exclude"] == ["귀화한"]
    assert u["age"]["basis"] == "노인복지법"
    assert (u["search_query"], u["dropped"]) == ("고령 인구", ["거주형태"])
    assert u["origin"] == {"region": "question", "year": "default", "sex": "guess"}


def test_review_is_kept_only_for_the_picked_family() -> None:
    by_id = {
        "A1": {"tbl_id": "A1", "family_id": "FA"},
        "A2": {"tbl_id": "A2", "family_id": "FA"},
        "B1": {"tbl_id": "B1", "family_id": "FB"},
    }
    raw = {"pick": "A2", "match": "exact", "reason": "모두 충족"}
    result = {"llm_pick": {"raw": raw, "pick": "A2", "llm": "gemma"}}
    got = table_guide._review_of(result, by_id["A1"], by_id)
    assert got == {"match": "exact", "reason": "모두 충족", "llm": "gemma"}
    result["llm_pick"]["pick"] = "B1"
    assert table_guide._review_of(result, by_id["A1"], by_id) is None
    assert table_guide._review_of({}, by_id["A1"], by_id) is None


def test_plan_conditions_use_understanding_shape() -> None:
    whole = {"region": "부산광역시"}
    got = table_guide._plan_conditions(
        {
            "region_level": "시군구",
            "year_from": 2020,
            "year_to": 2024,
            "sex": "남녀",
            "age": "65세 이상",
        },
        whole,
    )
    assert got == {
        "region": "부산광역시",
        "region_level": "시군구",
        "year": 2020,
        "year_end": 2024,
        "sex": None,
        "items": [],
        "classes": [],
        "age_text": "65세 이상",
    }
    one_year = table_guide._plan_conditions({"year_from": 2023, "year_to": 2023}, whole)
    assert (one_year["year"], one_year["year_end"]) == (2023, None)
