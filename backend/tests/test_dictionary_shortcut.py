"""사전 지름길 (D1): 질문에 지표 이름이 글자로 없어 '근거 없음'이 된 사전 답은 바로 넘기지 않고
사전 게이트가 조건을 검사한다. 조건을 모두 담았으면 사전 답을 둔다."""

import asyncio
from datetime import date

import pytest

from app.services import dict_binding, qa

TODAY = date(2026, 11, 26)


def _parsed(**kw):
    base = {"type": "value", "indicator": None, "regions": [], "year": None, "sex": "계"}
    return {**base, **kw}


@pytest.mark.needs_data
def test_missing_name_is_tagged_so_the_gate_can_check(monkeypatch) -> None:
    monkeypatch.setattr(qa.indicators, "find_in_text", lambda text: [])
    p = qa.correct("전국에서 들어온 사람이 나간 사람보다 많아?", _parsed(indicator="순이동"), TODAY)
    assert p["grounded"] is False and p["ungrounded_by"] == "name"


@pytest.mark.needs_data
def test_explicit_reason_is_not_the_name_tag(monkeypatch) -> None:
    p = qa.correct("2024년 부산 외국인 인구 몇 명이야?", _parsed(indicator="총인구"), TODAY)
    assert p["grounded"] is False and p["ungrounded_by"] != "name"


def _answer(monkeypatch, parsed, decision, *, settings_gate=True):
    calls = []

    async def compute(p, trace):
        return {"status": "ok", "indicator": p["indicator"], "value": 1, "year": 2025}

    async def shadow(question, p, result):
        calls.append(question)
        return {"decision": decision, "reasons": ["x"] if decision == dict_binding.HAND_OFF else []}

    monkeypatch.setattr(qa, "_compute_fetching", compute)
    monkeypatch.setattr(qa, "summary", lambda r: "")
    monkeypatch.setattr(qa, "render", lambda r: "")
    monkeypatch.setattr(qa, "sources", lambda r: [])
    monkeypatch.setattr(dict_binding, "shadow", shadow)
    monkeypatch.setattr(qa.ask_followup, "needs_year", lambda q: False)
    settings = qa.get_settings()
    monkeypatch.setattr(settings, "binding_dict_gate", settings_gate)
    monkeypatch.setattr(settings, "binding_dict_shadow", False)
    res = asyncio.run(qa.answer(parsed, {}, "질문"))
    return res["parsed"], calls


def test_name_only_ungrounded_is_kept_when_conditions_bind(monkeypatch) -> None:
    parsed = _parsed(indicator="순이동", grounded=False, ungrounded_by="name")
    p, calls = _answer(monkeypatch, parsed, dict_binding.KEEP)
    assert calls and p["grounded"] is True and p["dictionary_shortcut"] is True


def test_name_only_ungrounded_is_handed_off_when_a_condition_is_left(monkeypatch) -> None:
    parsed = _parsed(indicator="출생아 수", grounded=False, ungrounded_by="name")
    p, calls = _answer(monkeypatch, parsed, dict_binding.HAND_OFF)
    assert calls and p["grounded"] is False and p["gate_reasons"] == ["x"]
    assert not p.get("dictionary_shortcut")


def test_hard_ungrounded_skips_the_gate(monkeypatch) -> None:
    parsed = _parsed(indicator="총인구", grounded=False, ungrounded_by="인구에 수식어")
    p, calls = _answer(monkeypatch, parsed, dict_binding.KEEP)
    assert not calls and p["grounded"] is False


def test_gate_off_keeps_the_old_hand_off(monkeypatch) -> None:
    parsed = _parsed(indicator="순이동", grounded=False, ungrounded_by="name")
    p, calls = _answer(monkeypatch, parsed, dict_binding.KEEP, settings_gate=False)
    assert not calls and p["grounded"] is False


@pytest.mark.parametrize(
    ("question", "route"),
    [
        ("작년에 서울에서 경기도로 이사 간 사람 몇 명이야?", True),
        ("다른 시도에서 경기도로 이사 온 사람 최근 5년 추이", False),
        ("타 지역에서 부산으로 옮겨 온 사람 몇 명이야?", False),
        ("경기도 밖에서 들어온 사람 몇 명이야?", False),
    ],
)
def test_generic_origin_is_total_inflow_not_a_route(question: str, route: bool) -> None:
    assert dict_binding.specific_route(question) is route
