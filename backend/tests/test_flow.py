"""처리 흐름 기록 (app/services/flow.py): 질문이 거친 단계와 순서, 답을 정한 단계"""

import asyncio

import pytest

from app.core.config import get_settings
from app.services import dynamic_indicator as d
from app.services import flow, indicators, qa


def test_add_without_start_does_nothing() -> None:
    flow._steps.set(None)
    flow.add("질문 이해", "HCX", "지표=출생아 수")
    assert flow.current() is None


def test_steps_are_kept_in_order_and_shown_as_lines() -> None:
    steps = flow.start()
    flow.add("질문 이해", "HCX", "지표=출생아 수")
    flow.add("사전 계산", "ok", "출생아 수 값=3명")
    flow.add("최종 답", "ok", "new · 2024년 서울의 출생아 수는 3명입니다.")
    assert [s["stage"] for s in steps] == ["질문 이해", "사전 계산", "최종 답"]
    assert flow.text(steps).splitlines()[1] == "2. 사전 계산 [ok] 출생아 수 값=3명"
    assert flow.deciding(steps)["stage"] == "사전 계산"


def test_deciding_step_of_a_failed_answer_is_the_last_failure() -> None:
    steps = flow.start()
    flow.add("질문 이해", "HCX", "")
    flow.add("사전 계산", "unknown_indicator", "")
    flow.add("통계표 찾기", "ok", "T1 표")
    flow.add("표 칸으로 계산", "못 함", "T1 · 질문의 조건 '미혼'을 표의 칸에 반영할 수 없음")
    flow.add("최종 답", "tables", "")
    assert flow.deciding(steps)["detail"].endswith("반영할 수 없음")


def test_requests_do_not_mix_and_concurrent_parts_share_one_list() -> None:
    async def part(name):
        flow.add("표 칸으로 계산", "ok", name)

    async def request(name):
        steps = flow.start()
        await asyncio.gather(part(f"{name}-A"), part(f"{name}-B"))  # 후보 표를 동시에 계산
        return steps

    async def both():
        return await asyncio.gather(request("1"), request("2"))

    one, two = asyncio.run(both())
    assert sorted(s["detail"] for s in one) == ["1-A", "1-B"]
    assert sorted(s["detail"] for s in two) == ["2-A", "2-B"]


def test_long_detail_is_cut() -> None:
    steps = flow.start()
    flow.add("통계표 찾기", "ok", "가" * 500)
    assert len(steps[0]["detail"]) == flow.DETAIL_MAX


@pytest.fixture
def hcx(monkeypatch):
    async def chat(prompt, system, temperature=0.1, trace=None):
        return '{"type": "value", "indicator": "총인구", "regions": ["서울"], "year": 2024}'

    def correct(question, parsed):
        return {**parsed, "corrections": ["year: 2023 → 2024 ('작년')"]}

    monkeypatch.setattr(qa, "_chat", chat)
    monkeypatch.setattr(indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa, "correct", correct)
    monkeypatch.setattr(get_settings(), "question_cache", True)
    qa.UNDERSTAND_CACHE.clear()
    yield
    qa.UNDERSTAND_CACHE.clear()


def test_understanding_is_recorded_with_code_fixes(hcx) -> None:
    steps = flow.start()
    asyncio.run(qa.understand("작년 서울 인구"))
    asyncio.run(qa.understand("작년 서울 인구"))
    assert [s["result"] for s in steps] == ["HCX", "캐시"]
    assert "지표=총인구" in steps[0]["detail"] and "지역=서울" in steps[0]["detail"]
    assert "코드 보정: year: 2023 → 2024" in steps[0]["detail"]


def test_table_attempt_is_recorded(monkeypatch) -> None:
    async def once(tbl_id, org_id, question, parsed, trace, forced, *a):
        trace["dynamic"].append({"tbl_id": tbl_id, "skip": "질문의 말과 맞는 칸이 없음"})
        return None, None

    monkeypatch.setattr(d, "_try_once", once)
    steps = flow.start()
    asyncio.run(d._try("T1", None, "서울 반려견 수", {}, {"dynamic": []}, {}))
    assert steps == [
        {"stage": "표 칸으로 계산", "result": "못 함", "detail": "T1 · 질문의 말과 맞는 칸이 없음"}
    ]


def test_dictionary_result_line() -> None:
    ok = {"status": "ok", "indicator": "출생아 수", "value": 3, "unit": "명", "tbl_id": "T"}
    assert qa.flow_result(ok).startswith("출생아 수 값=3명")
    assert qa.flow_result({"status": "out_of_range", "reason": "수록 기간 밖"}) == "수록 기간 밖"


def test_devset_csv_explains_wrong_answer_with_deciding_step() -> None:
    from scripts import devset_grade as dg
    from scripts import eval_devset

    steps = [
        {"stage": "질문 이해", "result": "HCX", "detail": "지표=미혼 1인 가구"},
        {"stage": "표 칸으로 계산", "result": "ok", "detail": "T1 · 칸=1인가구 (규칙)"},
        {"stage": "최종 답", "result": "ok", "detail": "new · …"},
    ]
    res = {"status": "ok", "trace": {"flow": steps}}
    g = dg.Grade(id="X-1", set_name="개발셋", expected="답변", actual="답변", reason="숫자 다름")
    cols = eval_devset.flow_columns(g, res, None)
    assert cols["처리 흐름"].splitlines()[1] == "2. 표 칸으로 계산 [ok] T1 · 칸=1인가구 (규칙)"
    deciding = "표 칸으로 계산 [ok] T1 · 칸=1인가구 (규칙)"
    assert cols["오답 설명"] == f"숫자 다름 ← 답을 정한 단계: {deciding}"
    g.correct = "O"
    assert eval_devset.flow_columns(g, res, None)["오답 설명"] == ""
    # 되묻기 뒤 답이 있으면 두 턴의 흐름을 이어 쓴다
    both = eval_devset.flow_columns(g, {"trace": {}}, res)["처리 흐름"]
    assert both.startswith("[첫 질문]\n(흐름 기록 없음)\n[되묻기 답]\n1. 질문 이해")
