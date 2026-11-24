import asyncio

import pytest

from app.services import binding, dict_binding
from app.services import dynamic_indicator as d
from scripts import dict_binding_compare as cmp

Dim = d.Dim


@pytest.mark.parametrize(
    ("question", "sub_year", "from_to"),
    [
        ("작년 상반기에 대구에서 결혼 몇 건 했어?", True, False),
        ("작년 1월이랑 7월 중에 돌아가신 분 언제가 더 많았어?", True, False),
        ("작년에 서울에서 경기도로 이사 간 사람 몇 명이야?", False, True),
        ("강남구 사람 몇 명 살아?", False, False),
        ("서울에서 인구 제일 많은 구", False, False),
    ],
)
def test_question_patterns(question: str, sub_year: bool, from_to: bool) -> None:
    assert bool(dict_binding.SUB_YEAR.search(question)) is sub_year
    assert bool(dict_binding.FROM_TO.search(question)) is from_to


def _run(monkeypatch, question, parsed, result, dims, understanding):
    monkeypatch.setattr(d, "schema", lambda tbl_id: (dims, None, "시도"))
    monkeypatch.setattr(binding.di, "schema", lambda tbl_id: (dims, None, "시도"))

    async def cond(q):
        return understanding

    monkeypatch.setattr(dict_binding, "_conditions", cond)
    return asyncio.run(dict_binding.shadow(question, parsed, result))


RESULT = {
    "status": "ok",
    "source": "출생 표 (DT_X)",
    "tbl_id": "DT_X",
    "basis": "각 연도 1월 1일 ~ 12월 31일",
}


def test_unbound_condition_hands_off(monkeypatch) -> None:
    items = Dim(0, "항목", {"T10": "출생건수", "T20": "사망건수"})
    monkeypatch.setattr(
        dict_binding.indicators,
        "spec",
        lambda n: {"sources": [{"table": "DT_X", "item_id": "T10"}]},
    )
    monkeypatch.setattr("app.services.qa.sources", lambda r: [{"tbl_id": "DT_X"}])
    u = {"topic": "출생아 수", "conditions": {"items": ["세쌍둥이 이상"], "classes": []}}
    rec = _run(
        monkeypatch,
        "작년에 세쌍둥이 이상으로 태어난 아기 몇 명이야?",
        {"indicator": "출생아 수"},
        RESULT,
        [items],
        u,
    )
    assert rec["decision"] == dict_binding.HAND_OFF and rec["forced"] == {"0": "T10"}


def test_plain_question_keeps_the_answer(monkeypatch) -> None:
    items = Dim(0, "항목", {"T10": "출생건수", "T20": "사망건수"})
    monkeypatch.setattr(
        dict_binding.indicators,
        "spec",
        lambda n: {"sources": [{"table": "DT_X", "item_id": "T10"}]},
    )
    monkeypatch.setattr("app.services.qa.sources", lambda r: [{"tbl_id": "DT_X"}])
    u = {
        "topic": "출생아 수",
        "conditions": {"items": [], "classes": ["성별"]},
    }
    rec = _run(
        monkeypatch, "작년에 태어난 아기 몇 명이야?", {"indicator": "출생아 수"}, RESULT, [items], u
    )
    assert rec["decision"] == dict_binding.KEEP and rec["reasons"] == []


def test_compare_script_puts_records_in_four_cells() -> None:
    recs = {
        "A": ("q", {"decision": "넘김", "reasons": ["r"], "tbl_id": "T"}),
        "B": ("q", {"decision": "그대로", "reasons": [], "tbl_id": "T"}),
        "C": ("q", {"error": "x"}),
    }
    rows = {"A": {"정답": "X", "틀린 이유": "표"}, "B": {"정답": "O"}, "C": {"정답": "O"}}
    by = cmp.classify(recs, rows)
    assert [k for k, _, _, _ in by["넘김 · 지금 오답"]] == ["A"]
    assert [k for k, _, _, _ in by["그대로 · 지금 정답"]] == ["B"]
    assert "| 넘김 · 지금 오답 | 1 | A |" in cmp.report(by, "x")
