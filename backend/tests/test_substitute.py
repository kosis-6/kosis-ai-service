import asyncio

import pytest

from app.services import ask_session, dynamic_indicator, figure, qa
from app.services import substitute as sb

LABELS = [
    ("11", "서울특별시"),
    ("11650", "서초구"),
    ("1165055000", "반포본동"),
    ("1165056000", "반포1동"),
    ("1165057000", "반포2동"),
    ("44", "충청남도"),
    ("4415034000", "반포면"),
    ("11680", "강남구"),
    ("1168051000", "신사동"),
    ("11440", "마포구"),
    ("1144055000", "신사동"),
]


def test_founded_region_moves_to_first_year():
    q = {"type": "value", "year": 1980}
    r = {"status": "region_not_in_year", "available_from": 2012, "region": "세종특별자치시"}
    swap = sb.plan(q, r)
    assert swap.change == {"year": 2012}
    assert swap.lead.startswith("세종특별자치시는 2012년에 생겨 1980년 자료가 없어")


def test_past_year_moves_to_first_year_but_future_does_not():
    r = {"status": "out_of_range", "available_years": [1970, 2025], "indicator": "이혼 건수"}
    swap = sb.plan({"type": "value", "year": 1965}, r)
    assert swap.change == {"year": 1970}
    assert "1965년 대신 가장 가까운 1970년" in swap.lead
    assert sb.plan({"type": "value", "year": 2030}, r) is None


def test_trend_and_already_substituted_are_left_alone():
    r = {"status": "out_of_range", "available_years": [1970, 2025]}
    assert sb.plan({"type": "trend", "year": 1965, "start_year": 1960}, r) is None
    assert sb.plan({"type": "value", "year": 1965, "substituted": True}, r) is None


def test_follow_goes_on_to_first_collected_year():
    q = {"type": "value", "year": 1990}
    first = sb.plan(q, {"status": "region_not_in_year", "available_from": 2003, "region": "계룡시"})
    again = {"status": "out_of_range", "available_years": [2008, 2025]}
    swap = sb.follow(first, {**q, **first.change}, again)
    assert swap.change == {"year": 2008}
    assert (swap.before, swap.after) == ("1990년", "2008년")
    assert swap.lead.startswith("계룡시는 2003년에 생겼고 자료는 2008년부터 있어 1990년 대신")


def test_nearest_band_prefers_most_overlap_by_ratio():
    bands = [(60, 64), (65, None), (65, 69), (70, 74), (75, 79)]
    assert sb.nearest_band((65, 72), bands, rate=True)[0] == (65, 69)
    assert sb.nearest_band((33, 38), [(30, 34), (35, 39)], rate=False)[0] == (35, 39)


def test_tied_bands_are_joined_for_counts_not_rates():
    bands = [(55, 59), (60, 64), (65, 69), (70, 74)]
    assert sb.nearest_band((62, 67), bands, rate=False)[0] == (60, 69)
    assert sb.nearest_band((62, 67), bands, rate=True)[0] == (60, 64)


def test_band_swap_uses_table_ranges():
    q = {"ages": [[33, 38]]}
    r = {
        "status": "band_mismatch",
        "available_band_ranges": [[30, 34], [35, 39]],
        "indicator": "등록외국인",
    }
    swap = sb.plan(q, r)
    assert swap.change["ages"] == [[35, 39]]
    assert swap.lead == "표에 33~38세 구간이 없어 가장 가까운 35~39세 값으로 답합니다."
    assert swap.others == ["30~34세"]


def test_dong_goes_up_to_its_sigungu_only_when_unique():
    assert sb.dong_parent("반포동", LABELS) == "서울특별시 서초구"
    assert sb.dong_parent("신사동", LABELS) is None  # 강남구 · 마포구 두 곳
    assert sb.dong_parent("서초구", LABELS) is None


def test_region_swap_from_unknown_dong():
    q = {"regions": ["반포동"]}
    swap = sb.plan(q, {"status": "unknown_region", "region": "반포동"}, LABELS)
    assert swap.change["regions"] == ["서울특별시 서초구"]
    assert swap.lead == "'반포동' 같은 읍면동 단위 자료는 없어 서초구 값으로 답합니다."


def test_table_region_swap_needs_region_level_only():
    def resolve(name, year=None):
        return {
            "status": "ok",
            "kind": "시군구",
            "sido_name": "울산광역시",
            "display": "울산광역시 중구",
        }

    q = {"regions": ["울산 중구"]}
    swap = sb.table_region_swap(q, {"region_level": False}, resolve, "미혼모")
    assert swap.change == {"regions": ["울산광역시"]}
    assert "시도 단위까지만 집계돼 울산광역시 값으로" in swap.lead
    assert sb.table_region_swap(q, {"region_level": False, "year": False}, resolve, "") is None


def test_table_band_swap_needs_age_items_only():
    checks = {"class:연령": True, "item:18살에서 23살 사이": False, "region_level": True}
    swap = sb.table_band_swap({"ages": [[18, 23]]}, checks, [(15, 19), (20, 24)], "장애인 인구")
    assert swap.change["ages"] == [[20, 24]]
    checks["target"] = False
    assert sb.table_band_swap({"ages": [[18, 23]]}, checks, [(15, 19), (20, 24)], "") is None


@pytest.mark.parametrize("item", ["미혼", "18살에서 23살 사이 미혼"])
def test_table_alternative_keeps_non_age_conditions(item, monkeypatch):
    def bands(_tbl_id):
        pytest.fail("연령 외 조건이 남은 표의 연령 구간을 조회했습니다")

    async def run(_entry, _parsed):
        pytest.fail("연령 외 조건을 바꾼 표로 계산했습니다")

    monkeypatch.setattr(dynamic_indicator, "_age_bands", bands)
    entry = {
        "tbl_id": "T1",
        "checks": {"class:연령": True, "item:18살에서 23살 사이": False, f"item:{item}": False},
    }
    assert (
        asyncio.run(dynamic_indicator._substituted([entry], {"ages": [[18, 23]]}, run, {})) is None
    )


def test_dictionary_alternative_reports_and_inherits_the_calculated_age(monkeypatch):
    def compute(parsed):
        if parsed["ages"] == [[33, 38]]:
            return {"status": "band_mismatch", "available_band_ranges": [[30, 34], [35, 39]]}
        assert parsed["ages"] == [[35, 39]]
        return {
            "status": "ok",
            "indicator": "등록외국인 수",
            "unit": "명",
            "year": 2024,
            "region": "서울",
            "value": 100,
            "tbl_id": "T1",
        }

    monkeypatch.setattr(qa, "_compute", compute)
    asked = {"type": "value", "ages": [[33, 38]], "year": 2024}
    res = asyncio.run(qa.answer(asked))
    assert asked["ages"] == [[33, 38]]
    assert res["result"]["changed_condition"]["from"] == "33~38세"
    assert figure.figure_of(res["result"], res["parsed"])["conditions"]["ages"] == "35~39세"
    assert ask_session.effective(res["parsed"], res["result"])["ages"] == [[35, 39]]


def test_rejected_dictionary_alternative_hands_off_the_original_age(monkeypatch):
    from app.services import dict_binding, table_guide

    async def compute(parsed, trace):
        return {
            "status": "ok",
            "indicator": "미혼 비율",
            "value": 38.62,
            "changed_condition": {"name": "연령", "applied": {"ages": [[35, 39]]}},
        }

    async def shadow(question, parsed, result):
        return {
            "decision": dict_binding.HAND_OFF,
            "reasons": ["질문 지표 불일치"],
            "requested_indicator": "등록외국인 수",
        }

    monkeypatch.setattr(qa, "_compute_fetching", compute)
    monkeypatch.setattr(dict_binding, "shadow", shadow)
    monkeypatch.setattr(qa.get_settings(), "binding_dict_gate", True)
    asked = {"type": "value", "indicator": "미혼", "grounded": True, "ages": [[33, 38]]}
    res = asyncio.run(qa.answer(asked, question="33~38세 미혼 등록외국인 수"))
    assert res["status"] == "unknown_indicator"
    assert res["parsed"]["ages"] == [[33, 38]]
    assert res["parsed"]["indicator"] == "등록외국인 수"
    assert "value" not in res["result"] and "changed_condition" not in res["result"]
    found = {"status": "ok", "answer": "요청하신 통계는 장래가구추계에서 확인할 수 있습니다."}
    fallback = table_guide.tables_answer(found, res)
    assert "모든 조건을 충족하는 수치를 확인하지 못했습니다" in fallback["answer"]
    assert fallback["tables"]["answer"] == fallback["answer"]


def test_mark_adds_field_and_lead():
    swap = sb.plan(
        {"type": "value", "year": 1965}, {"status": "out_of_range", "available_years": [1970, 2025]}
    )
    result = sb.mark({"status": "ok", "value": 1}, swap)
    assert result["changed_condition"]["to"] == "1970년"
    assert result["lead"] == swap.lead
    res = sb.mark_response({"answer": "1970년 값은 1입니다.", "result": {}}, swap)
    assert res["answer"].startswith(swap.lead + "\n")
    assert res["parsed"]["year"] == 1970
