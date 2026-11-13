"""2026-11-13에 찾아 고친 결함 (eval/results/defects_20261113.md)

결함마다 고친 동작을 확인하는 테스트와, 고치면서 깨지면 안 되는 동작을 지키는 테스트를 함께 둔다.
DB·HCX 없이 돈다 (지표 사전은 test_qa_correct와 같은 방식으로 바꿔 끼운다).
"""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.agent import multi
from app.agent import plan as planner
from app.main import app
from app.services import indicators, qa, search, synonyms
from tests.test_qa import _region_trend

TODAY = date(2026, 11, 13)
ALIASES = {"출생아 수": ["출생 수", "태어난 아이"]}


@pytest.fixture(autouse=True)
def fixed_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", lambda: ALIASES)


def parsed(**kw) -> dict:
    base = {"type": "value", "indicator": "출생아 수", "regions": [], "year": None, "sex": "계"}
    return {**base, **kw}


# ── 결함 1: 여러 지역 합계 ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        # 원래 잡던 표현 (고치면서 깨지면 안 된다)
        "2024년 부산 울산 경남 출생아 수 합쳐서 몇 명이야",
        "2024년 부산 울산 경남 출생아 수 총 몇 명",
        "2024년 부산 울산 경남 출생아 수 모두 더하면",
        # 2026-11-13에 놓치던 표현
        "2024년 부산 울산 경남 다 해서 출생아 수",
        "2024년 부산이랑 울산 출생아 전부 몇 명",
        "2024년 부산이랑 울산 출생아 모두 몇 명",
        "2024년 부산 울산 경남 통틀어 출생아 수",
    ],
)
def test_sum_words_for_regions(question: str) -> None:
    p = qa.correct(question, parsed(regions=["부산", "울산", "경남"], year=2024), TODAY)
    assert (p["type"], p.get("aggregate")) == ("value", "sum")


def test_value_with_several_regions_and_no_sum_is_a_comparison() -> None:
    """합계를 묻지 않았는데 HCX가 value로 읽으면 첫 지역만 답했다 → 지역별 비교로"""
    p = qa.correct("2024년 부산 울산 출생아 수", parsed(regions=["부산", "울산"], year=2024), TODAY)
    assert (p["type"], p["regions"]) == ("compare", ["부산", "울산"])


def test_compare_with_sum_words_is_summed() -> None:
    """HCX가 '합쳐서'를 compare로 읽어도 합계로 (compare에서는 더하지 않았다)"""
    q = "2024년 부산 울산 경남 출생아 수 합쳐서"
    p = qa.correct(q, parsed(type="compare", regions=["부산", "울산", "경남"], year=2024), TODAY)
    assert (p["type"], p.get("aggregate")) == ("value", "sum")


def test_one_region_value_is_left_alone() -> None:
    p = qa.correct("2024년 부산 출생아 수", parsed(regions=["부산"], year=2024), TODAY)
    assert (p["type"], p.get("aggregate")) == ("value", None)


@pytest.mark.parametrize(
    "question",
    [
        "2024년 부산 울산 합계출산율",
        "시도 전부 보여줘 출생아 수",
        "2024년 출생아 수 모두 보여줘",
        # 합계 표현을 넓힌 뒤 리뷰에서 나온 오탐: '보다'의 '다', '모두·전부' 뒤의 '합계출산율'
        "부산과 대구 중 서울보다 합계출산율이 높은 곳은?",
        "서울과 부산 중 어디가 작년보다 합계출산율이 올랐어?",
        "서울 부산 모두 합계출산율이 낮아?",
        "전부 합계출산율 보여줘",
    ],
)
def test_not_a_sum(question: str) -> None:
    """'합계출산율'의 합계, 목록을 뜻하는 '전부'·'모두', '○○보다'는 합산이 아니다"""
    assert not qa.SUM_WORDS.search(question)


def test_comparison_with_boda_stays_a_comparison() -> None:
    """'서울보다 합계출산율이 높은 곳'은 세 지역 비교.
    합계로 읽으면 합계출산율을 더한 무의미한 값을 답한다"""
    q = "부산과 대구 중 서울보다 합계출산율이 높은 곳은?"
    p = qa.correct(
        q, parsed(type="compare", indicator="합계출산율", regions=["부산", "대구", "서울"]), TODAY
    )
    assert (p["type"], p.get("aggregate")) == ("compare", None)


# ── 결함 2: 기간 합계 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "question",
    [
        "최근 5년 출생아 수 총 몇 명",
        "최근 5년 전국 출생아 수 누적",
        "최근 5년 동안 전국에서 태어난 아이 다 합치면",
    ],
)
def test_sum_words_for_periods(question: str) -> None:
    p = qa.correct(question, parsed(type="trend", regions=["전국"]), TODAY)
    assert (p["type"], p.get("aggregate")) == ("trend", "sum")


def _period_sum(region: str, value: int) -> dict:
    """지역 하나의 기간 합계 결과 (qa._sum_years가 만든 모양)"""
    return {
        "status": "ok",
        "indicator": "출생아 수",
        "kind": "count",
        "unit": "명",
        "region": region,
        "year": "2021~2025",
        "value": value,
        "members": [{"region": "2021년", "value": value}],
        "notes": ["2021~2025년(5개년) 합계"],
    }


def test_period_sum_over_several_regions_adds_every_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'수도권 최근 5년 출생아 수 합계'는 첫 지역(서울)만 셌다 → 지역마다 기간 합계를 내고 더한다"""
    sums = {"서울": 200, "인천": 80, "경기": 400}
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "spec", lambda name: {"name": name, "method": "lookup"})
    monkeypatch.setattr(
        qa, "_compute_lookup", lambda q, sp, kind, rs, *a: _period_sum(rs[0], sums[rs[0]])
    )
    q = {
        "type": "trend",
        "indicator": "출생아 수",
        "regions": ["서울", "인천", "경기"],
        "group": "수도권",
        "aggregate": "sum",
    }
    r = qa.compute(q)
    assert (r["status"], r["region"], r["value"]) == ("ok", "수도권", 680)
    assert [m["region"] for m in r["members"]] == ["서울", "인천", "경기"]


def test_period_without_sum_per_region_is_shown_per_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기간 합계를 낼 수 없는 지표(인구처럼 쌓이지 않는 값)는 더하지 않고 지역별 추이로"""
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa.indicators, "spec", lambda name: {"name": name, "method": "lookup"})
    monkeypatch.setattr(
        qa, "_compute_lookup", lambda q, sp, kind, rs, *a: _region_trend(rs[0], [10, 9])
    )
    q = {"type": "trend", "indicator": "출생아 수", "regions": ["서울", "인천"], "aggregate": "sum"}
    r = qa.compute(q)
    assert r["split_by"] == "region" and [p["region"] for p in r["split"]] == ["서울", "인천"]


# ── 결함 3: 두 표가 필요한 질문을 하나로 합침 ──────────────────────────

FUTURE_VS_NOW = "2050년 총인구 전망을 지금 주민등록인구와 비교해 줘"


def _plan(*spans: str) -> dict:
    """2026-11-13 실제 계획 LLM 출력 모양: 질문의 연도를 공통 조건(shared)에 넣었다"""
    return {
        "shared": {"region_level": "없음", "year_from": 2050, "year_to": 2050},
        "indicators": [
            {"span": s, "search": s, "scope": "in", "role": "measure", "override": {}}
            for s in spans
        ],
    }


def _conditions(p: dict) -> dict:
    return {
        i["span"]: planner.merged_conditions(
            p["shared"], i.get("override"), i.get("no_inherit") or ()
        )
        for i in p["indicators"]
    }


def test_present_indicator_does_not_inherit_the_future_year() -> None:
    p = planner.correct(FUTURE_VS_NOW, _plan("현재 주민등록인구", "2050년 총인구 전망"), TODAY)
    cond = _conditions(p)
    assert "year_from" not in cond["현재 주민등록인구"]
    assert (cond["2050년 총인구 전망"]["year_from"], cond["2050년 총인구 전망"]["year_to"]) == (
        2050,
        2050,
    )


def test_present_word_right_before_the_span_counts() -> None:
    """측정 x02: '…시도 3곳의 현재 평균 초혼 연령' (구절 바로 앞에 '현재')"""
    q = "2050년에 고령인구 비중이 가장 높을 것으로 예상되는 시도 3곳의 현재 평균 초혼 연령은"
    p = planner.correct(q, _plan("고령인구 비중", "평균 초혼 연령"), TODAY)
    cond = _conditions(p)
    assert "year_from" not in cond["평균 초혼 연령"]
    assert cond["고령인구 비중"]["year_from"] == 2050


def test_indicators_without_present_word_still_inherit_the_year() -> None:
    q = "2050년 총인구 전망과 고령인구 비중"
    p = planner.correct(q, _plan("총인구 전망", "고령인구 비중"), TODAY)
    assert all(c.get("year_from") == 2050 for c in _conditions(p).values())


def test_indicators_with_different_years_are_not_merged() -> None:
    """1위 후보가 같은 표 묶음이어도 연도 조건이 다르면 다른 표가 필요하다"""
    inds = [
        {"span": "현재 주민등록인구", "conditions": {}, "candidates": [{"family_id": "F_PROJ"}]},
        {
            "span": "2050년 총인구 전망",
            "conditions": {"year_from": 2050, "year_to": 2050},
            "candidates": [{"family_id": "F_PROJ"}],
        },
    ]
    kept, merged = multi.merge_same(inds)
    assert [i["span"] for i in kept] == ["현재 주민등록인구", "2050년 총인구 전망"]
    assert merged == []


def test_indicators_with_same_years_are_still_merged() -> None:
    """원래 합치기('합의 이혼'·'재판 이혼')는 그대로"""
    cond = {"year_from": 2024, "year_to": 2024}
    inds = [
        {"span": "합의 이혼", "conditions": cond, "candidates": [{"family_id": "F1"}]},
        {"span": "재판 이혼", "conditions": dict(cond), "candidates": [{"family_id": "F1"}]},
    ]
    kept, merged = multi.merge_same(inds)
    assert [i["span"] for i in kept] == ["합의 이혼"] and merged == [["합의 이혼", "재판 이혼"]]


# ── 결함 4: Kiwi가 없을 때 알림 ────────────────────────────────────────


def test_without_kiwi_text_is_left_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kiwi가 없으면 조사를 떼지 않고 원문을 그대로 쓴다 (오류는 내지 않는다)"""
    monkeypatch.setattr(synonyms, "_KIWI", None)
    assert synonyms.kiwi_clean("시도별로 혼자 사는 1인가구") == "시도별로 혼자 사는 1인가구"


@pytest.mark.parametrize(("kiwi", "expected"), [(object(), "kiwi"), (None, "regex")])
def test_health_search_reports_morph_analyzer(
    monkeypatch: pytest.MonkeyPatch, kiwi: object, expected: str
) -> None:
    async def no_load() -> list:
        return []

    monkeypatch.setattr(search, "load_tables", no_load)
    monkeypatch.setattr(synonyms, "_KIWI", kiwi)
    body = TestClient(app).get("/health/search").json()
    assert body["morph"] == expected
