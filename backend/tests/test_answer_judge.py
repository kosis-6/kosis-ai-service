"""답 판정 (ANSWER_JUDGE=true): 판정에 보여 줄 요약, 다시 계산할 후보, 검토 건너뛰기"""

import asyncio
from datetime import date

import pytest

from app.core.config import get_settings
from app.services import answer_judge, ask_session, llm_pick


def trend(first: int, last: int) -> dict:
    return {
        "status": "ok",
        "indicator": "총인구",
        "source": "주민등록인구 (DT_X)",
        "region": "부산광역시",
        "series": [{"year": y, "value": 1} for y in range(first, last + 1)],
        "change": {"diff": 1, "rate": 0.1},
        "unit": "명",
    }


def test_describe_tells_the_shape_of_a_trend() -> None:
    text = answer_judge.describe(trend(2019, 2023))
    assert "형태: 연도별 값과 처음 → 끝 변화량 · 증감률" in text
    assert "기간: 2019~2023년 (5개 연도)" in text
    assert "연도:" not in text


def test_describe_ranking_says_how_many_are_shown() -> None:
    result = {
        "status": "ok",
        "indicator": "혼인 건수",
        "level": "시도",
        "order": "낮은 순",
        "total_regions": 17,
        "ranking": [{"region": n} for n in ("세종", "제주", "울산", "광주")],
        "year": 2024,
    }
    text = answer_judge.describe(result, {"top": 3})
    assert "시도 순위 (낮은 순), 전체 17곳 중 상위 3곳을 답함 (위에서부터 세종, 제주, 울산)" in text
    assert "연도: 2024년" in text


def test_describe_split_answer_and_single_value() -> None:
    one = {"status": "ok", "indicator": "사망자 수", "year": 2024, "sex": "남", "unit": "명"}
    other = {**one, "sex": "여"}
    text = answer_judge.describe({"status": "ok", "split": [one, other]})
    assert text.startswith("(답을 남 · 여 따로 계산해 함께 보여 줌: 2개)")
    assert text.count("형태: 값 하나") == 2
    assert "성별: 남" in text


def test_describe_is_none_without_a_value_answer() -> None:
    assert answer_judge.describe(None) is None
    assert answer_judge.describe({"status": "out_of_range"}) is None
    assert answer_judge.user_text("질문", {}, {"status": "missing_data"}) is None


def test_time_read_uses_the_last_year_actually_used() -> None:
    parsed = {"time_refs": [{"expr": "최근 5년", "kind": "recent", "start": 2021, "end": None}]}
    assert answer_judge.time_read(parsed, trend(2022, 2026)) == (
        "'최근 5년' → 가장 최근 자료(2026년)까지"
    )
    assert answer_judge.time_read(parsed, {"status": "ok"}) == (
        "'최근 5년' → 2021년부터 가장 최근 자료까지"
    )
    said = {
        "time_refs": [{"expr": "작년", "year": 2025}, {"expr": "x", "start": 2001, "end": 2005}]
    }
    assert answer_judge.time_read(said, None) == "'작년' → 2025년, 'x' → 2001~2005년"
    assert answer_judge.time_read({}, None) == ""


def test_user_text_has_today_question_and_summary() -> None:
    parsed = {"time_refs": [{"expr": "작년", "year": 2025}]}
    result = {"status": "ok", "indicator": "출생아 수", "year": 2025}
    text = answer_judge.user_text("작년 출생아 수", parsed, result, date(2026, 3, 2))
    lines = text.split("\n")
    assert lines[0].startswith("오늘: 2026-03-02 (올해 2026년.")
    assert lines[1:4] == [
        "질문: 작년 출생아 수",
        "질문에서 읽은 시점: '작년' → 2025년",
        "결과: 답함",
    ]


def test_verdict_is_none_when_the_llm_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(text: str):
        raise RuntimeError("down")

    monkeypatch.setattr(answer_judge, "_ask", boom)
    result = {"status": "ok", "indicator": "출생아 수", "year": 2025}
    assert asyncio.run(answer_judge.verdict("q", {}, result)) is None


def test_verdict_reads_the_llm_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def said(text: str):
        return {"reason": "형태가 다름", "missing": "순위를 물었는데 값 하나", "verdict": "retry"}

    monkeypatch.setattr(answer_judge, "_ask", said)
    out = asyncio.run(answer_judge.verdict("q", {}, {"status": "ok", "indicator": "x"}))
    assert (out["verdict"], out["reason"]) == ("retry", "형태가 다름")


def test_enabled_needs_the_switch_and_a_gemma_address(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "answer_judge", True)
    monkeypatch.setattr(settings, "gemma_base_url", "")
    assert answer_judge.enabled() is False
    monkeypatch.setattr(settings, "gemma_base_url", "http://gemma/v1")
    assert answer_judge.enabled() is True
    monkeypatch.setattr(settings, "answer_judge", False)
    assert answer_judge.enabled() is False


def found_with(*ids: str, targets: int = 1) -> dict:
    cands = [
        {"tbl_id": i, "picked": n == 0, "review": {"match": "exact"}} for n, i in enumerate(ids)
    ]
    return {"handoff": {"targets": [{"candidates": list(cands)} for _ in range(targets)]}}


def test_without_tables_drops_the_used_table_and_the_pick() -> None:
    found = found_with("A", "B", "C")
    out = ask_session.without_tables(found, {"A"})
    rest = out["handoff"]["targets"][0]["candidates"]
    assert [c["tbl_id"] for c in rest] == ["B", "C"]
    assert all(c["picked"] is False and c["review"] is None for c in rest)
    assert len(found["handoff"]["targets"][0]["candidates"]) == 3  # 원래 꾸러미는 그대로


def test_without_tables_gives_up_when_nothing_changes() -> None:
    assert ask_session.without_tables(found_with("A"), {"A"}) is None  # 남는 후보 없음
    assert ask_session.without_tables(found_with("A", "B"), {"Z"}) is None  # 쓴 표가 후보에 없음
    assert ask_session.without_tables(found_with("A", "B", targets=2), {"A"}) is None
    assert ask_session.without_tables({}, {"A"}) is None


def test_judgeable_only_value_answers_to_whole_questions() -> None:
    ok = {"status": "ok", "request_type": ask_session.NEW, "result": {"status": "ok"}}
    assert ask_session.judgeable(ok)
    assert ask_session.judgeable({**ok, "request_type": ask_session.REPLY})
    assert not ask_session.judgeable({**ok, "request_type": ask_session.CHANGE})
    assert not ask_session.judgeable({**ok, "status": "tables"})
    assert not ask_session.judgeable({**ok, "clarification": {"kind": "table_cell"}})


def searched(gap: float, checks: dict, status: str = "ok", family: str = "F1") -> tuple[dict, list]:
    cands = [{"tbl_id": "A", "family_id": "F1"}, {"tbl_id": "B", "family_id": "F2"}]
    result = {
        "status": status,
        "table": {"tbl_id": "A", "family_id": family},
        "candidates": [
            {"tbl_id": "A", "score": 1.0, "checks": checks},
            {"tbl_id": "B", "score": 1.0 - gap, "checks": {}},
        ],
    }
    return result, cands


def test_sure_first_needs_all_checks_and_a_clear_gap() -> None:
    yes = {"region_level": True, "year": True}
    assert llm_pick.sure_first(*searched(0.2, yes))
    assert not llm_pick.sure_first(*searched(0.1, yes))  # 점수 차가 작음
    assert not llm_pick.sure_first(*searched(0.2, {**yes, "sex": False}))  # 조건 ✘
    assert not llm_pick.sure_first(*searched(0.2, {}))  # 확인한 조건이 없음
    assert not llm_pick.sure_first(*searched(0.2, yes, status="partial_match"))
    assert not llm_pick.sure_first(*searched(0.2, yes, family="F9"))  # 정한 표가 1위가 아님
    result, cands = searched(0.2, yes)
    assert llm_pick.sure_first(result, cands[:1])  # 후보가 하나
    assert not llm_pick.sure_first(result, [])
