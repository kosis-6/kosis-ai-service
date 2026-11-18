"""조건을 모두 만족하는 값이 없을 때의 대안 (alternative.py):
대안 만들기 · LLM 판단 반영 · 되묻기 모양

LLM · DB 없이 돈다. 예문은 평가 문항이 아니다.
"""

import asyncio

import pytest

from app.services import alternative as alt


def _entry(tbl_id, kind="other", checks=None, years=(2015, 2024), name=None):
    return {
        "tbl_id": tbl_id,
        "tbl_nm": name or f"{tbl_id} 표",
        "kind": kind,
        "checks": checks if checks is not None else {"year": True},
        "start_year": years[0],
        "end_year": years[1],
    }


Q = {"type": "value", "year": 2026, "regions": ["서울특별시"], "sex": "계"}


def test_year_change_moves_to_nearest_year_in_the_table() -> None:
    assert alt.year_change(_entry("A"), Q) == (
        {"year": 2024},
        "연도 2026년 → 2024년 (수록 2015~2024년)",
    )
    assert alt.year_change(_entry("A"), {**Q, "year": 2010})[0] == {"year": 2015}
    assert alt.year_change(_entry("A"), {**Q, "year": 2020}) is None  # 기간 안: 바꿀 것 없음
    trend = {"type": "trend", "start_year": 2020, "end_year": 2026}
    assert alt.year_change(_entry("A"), trend)[0] == {"start_year": 2020, "end_year": 2024}
    assert alt.year_change(_entry("A"), {**trend, "start_year": 2030}) is None  # 겹치는 해 없음


def test_plan_makes_year_and_other_table_alternatives_only() -> None:
    attempts = [
        {
            "entry": _entry("P", "picked", {"year": False}),
            "res": None,
            "skip": "조건 불충족 (year)",
        },
        {"entry": _entry("X", "other", {"year": False, "sex": False}), "res": None, "skip": "x"},
        {"entry": _entry("R", "other"), "res": {"status": "out_of_range"}, "skip": None},
        {"entry": _entry("O", "other"), "res": None, "skip": "검토가 고른 표를 '모두 충족'으로 봄"},
        {
            "entry": _entry("N", "other"),
            "res": None,
            "skip": None,
        },  # 계산했는데 칸이 없음: 대안 아님
    ]
    planned = alt.plan(attempts, Q, "P 표")
    assert [(p["entry"]["tbl_id"], p["change"]) for p in planned] == [
        ("P", {"year": 2024}),  # 연도 ✘만 있는 표 → 가까운 연도 (우선)
        ("R", {"year": 2024}),  # 수록 기간 밖으로 끝난 표 → 가까운 연도
        ("O", {}),  # 규칙으로 건너뛴 다른 표 → 그대로
    ]
    assert planned[2]["what"] == "다른 표 (고른 표 'P 표'로는 계산하지 못함)"


@pytest.fixture(autouse=True)
def alternatives_on(monkeypatch):
    """대안 기능은 기본 꺼짐(11/18). 이 파일은 켠 상태의 동작을 본다"""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "handoff_alternatives", True)


def test_alternatives_are_off_by_default() -> None:
    from app.core.config import Settings

    assert Settings.model_fields["handoff_alternatives"].default is False


@pytest.fixture
def run_ok():
    calls = []

    async def run(entry, q):
        calls.append((entry["tbl_id"], q.get("year")))
        return {
            "status": "ok",
            "answer": f"{q.get('year')}년 서울 {entry['tbl_id']} 100명\n\n자세히",
            "result": {"status": "ok", "value": 100},
            "parsed": q,
            "trace": {"big": True},
        }

    return run, calls


def _attempts():
    return [
        {
            "entry": _entry("P", "picked", {"year": False}),
            "res": None,
            "skip": "조건 불충족 (year)",
        },
        {"entry": _entry("O", "other"), "res": None, "skip": "검토가 고른 표를 '모두 충족'으로 봄"},
    ]


def test_offer_asks_with_values_for_what_the_llm_picked(monkeypatch, run_ok) -> None:
    run, calls = run_ok
    seen = {}

    async def judge(question, failures, alts):
        seen["alts"] = [(a["id"], a["what"], a["value"]) for a in alts]
        seen["failures"] = failures
        return {
            "action": "offer",
            "offers": [{"id": "A1", "label": "2024년 기준"}],
            "reason": "r",
        }, "gemma"

    monkeypatch.setattr(alt, "judge", judge)
    trace: dict = {}
    res = asyncio.run(alt.offer("2026년 서울 인구", Q, _attempts(), "P 표", run, trace))
    assert sorted(calls) == [("O", 2026), ("P", 2024)]  # 대안마다 값을 미리 계산 (동시에)
    assert seen["alts"][0] == (
        "A1",
        "연도 2026년 → 2024년 (수록 2015~2024년)",
        "2024년 서울 P 100명",
    )
    assert seen["failures"][0] == "[P 표] 조건 불충족 (year)"
    assert res["status"] == "clarify_question"
    c = res["clarification"]
    assert c["kind"] == "alternative" and c["options"] == ["2024년 기준"]
    assert res["answer"].splitlines()[1] == "① 2024년 기준: 2024년 서울 P 100명"
    stored = c["context"]["answers"][0]
    assert stored["result"]["value"] == 100 and "trace" not in stored  # 고르면 그대로 줄 답
    assert c["context"]["codes"] == {"2024년 기준": "0"}
    assert trace["alternative"]["offered"] == ["P"] and trace["alternative"]["llm"] == "gemma"


@pytest.mark.parametrize(
    "decided",
    [
        {"action": "tables", "offers": [], "reason": "대상이 다름"},
        {"action": "clarify", "offers": [], "question": "?", "reason": "r"},
        {"action": "offer", "offers": [{"id": "A9", "label": "없는 번호"}], "reason": "r"},
        None,  # 해석 실패
    ],
)
def test_offer_falls_back_when_llm_offers_nothing_usable(monkeypatch, run_ok, decided) -> None:
    async def judge(question, failures, alts):
        return decided, "gemma"

    monkeypatch.setattr(alt, "judge", judge)
    assert asyncio.run(alt.offer("q", Q, _attempts(), "P 표", run_ok[0], {})) is None


def test_no_alternative_means_no_llm_call(monkeypatch) -> None:
    async def judge(*args):
        raise AssertionError("대안이 없으면 LLM을 부르지 않는다")

    async def run_fail(entry, q):
        return None

    monkeypatch.setattr(alt, "judge", judge)
    trace: dict = {}
    assert asyncio.run(alt.offer("q", Q, _attempts(), "P 표", run_fail, trace)) is None
    assert trace["alternative"]["computed"] == []
    nothing = [{"entry": _entry("N", "other"), "res": None, "skip": None}]
    assert asyncio.run(alt.offer("q", Q, nothing, None, run_fail, {})) is None


def test_switch_turns_alternatives_off(monkeypatch, run_ok) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "handoff_alternatives", False)
    assert asyncio.run(alt.offer("q", Q, _attempts(), "P 표", run_ok[0], {})) is None


def test_user_text_lists_failures_and_alternatives() -> None:
    alts = [
        {"id": "A1", "entry": {"tbl_nm": "T"}, "what": "연도 2026년 → 2024년", "value": "100명"}
    ]
    text = alt.user_text("질문", ["[T] 이유"], alts)
    assert text == (
        "질문: 질문\n원래 조건으로 계산하지 못한 이유:\n- [T] 이유\n대안:\n"
        "A1. 표: T / 달라진 점: 연도 2026년 → 2024년 / 값의 기간: 연간(1년 전체) / 값: 100명"
    )
    # 질문이 1년보다 짧은 기간을 물었으면 달라진 점에 기간 단위 차이를 적는다
    half = alt.user_text("2024년 상반기 서울 혼인 건수", [], alts)
    assert "달라진 점: 연도 2026년 → 2024년. 기간 단위: 상반기 → 연간(1년 전체)" in half


@pytest.mark.parametrize(
    ("question", "period"),
    [
        ("2024년 상반기 서울 혼인 건수", "상반기"),
        ("2025년 2분기 사망자 수", "2분기"),
        ("지난달 혼인 건수", "지난달"),
        ("최근 3개월 출생아수", "최근 3개월"),
        ("올해 3월 출생아 수", "3월"),
        ("2024년 서울 혼인 건수", None),
        ("1990년생 인구", None),
    ],
)
def test_asked_period(question: str, period: str | None) -> None:
    assert alt.asked_period(question) == period
