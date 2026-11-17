"""동적 지표 2단계: 일상어 → 칸 이름 (cell_words.py). HCX는 가짜로"""

import asyncio

import pytest

from app.services import cell_words as cw
from app.services import dynamic_indicator as d
from app.services import qa
from app.services.hcx_client import HcxError

ITEM = d.Dim(0, "항목", {"T00": "상주인구", "T10": "유입 인구-계", "T30": "주간 인구"})
SEX = d.Dim(2, "성별", {"0": "계", "1": "남자", "2": "여자"})
NAMES = {"상주인구", "유입 인구-계", "주간 인구"}
Q = "중구는 낮에 사람이 밤보다 몇 배 많아?"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cw.CACHE.clear()


def test_pairs_matches_names_ignoring_spaces() -> None:
    text = '{"pairs": [{"word": "낮", "cell": "주간 인구"}, {"word": "밤", "cell": "상주 인구"}]}'
    assert cw._pairs(text, Q, NAMES) == [("낮", "주간 인구"), ("밤", "상주인구")]


@pytest.mark.parametrize(
    "text",
    [
        '{"pairs": [{"word": "낮", "cell": "주간 인구"}, {"word": "밤", "cell": "야간 인구"}]}',
        '{"pairs": [{"word": "아침", "cell": "주간 인구"}]}',  # 질문에 없는 말
        '{"pairs": [{"word": "낮", "cell": "주간 인구"}, {"word": "낮에", "cell": "상주인구"}]}',
        "모르겠습니다",
    ],
)
def test_pairs_drops_all_when_any_pair_is_wrong(text: str) -> None:
    # 짝 하나만 쓰면 "낮이 밤보다 몇 배"를 낮 인구 하나로 답하게 된다
    assert cw._pairs(text, Q, NAMES) == []


def test_menu_skips_totals_and_given_dims() -> None:
    menu, names = cw._menu([ITEM, SEX], skip={2})
    assert menu == "- 항목: 상주인구 | 유입 인구-계 | 주간 인구"
    assert names == NAMES


def test_match_rewrites_question_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def fake_chat(prompt, system, temperature=0.1, trace=None):
        calls.append(prompt)
        return (
            '{"pairs": [{"word": "낮", "cell": "주간 인구"}, {"word": "밤", "cell": "상주인구"}]}'
        )

    monkeypatch.setattr(qa, "_chat", fake_chat)
    trace: dict = {}
    got = asyncio.run(cw.match("DT_X", "상주(야간) · 주간 인구", Q, [ITEM, SEX], {2}, trace))
    assert got == (
        "중구는 주간 인구에 사람이 상주인구보다 몇 배 많아?",
        [("낮", "주간 인구"), ("밤", "상주인구")],
    )
    assert trace["cell_words"][0]["pairs"] == got[1]
    asyncio.run(cw.match("DT_X", "상주(야간) · 주간 인구", Q, [ITEM, SEX], {2}, {}))
    assert len(calls) == 1  # 같은 표·질문은 다시 묻지 않는다


def test_match_hcx_error_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def down(*args, **kwargs):
        raise HcxError("network", "연결 실패")

    monkeypatch.setattr(qa, "_chat", down)
    assert asyncio.run(cw.match("DT_X", "표", Q, [ITEM], set(), {})) is None


def test_note() -> None:
    assert cw.note([("자가용", "승용차 소형 승합차")]) == (
        "'자가용'을 '승용차 소형 승합차' 칸으로 보고 계산했습니다"
    )
    assert cw.note([("낮", "주간 인구"), ("밤", "상주인구")]) == (
        "'낮'을 '주간 인구', '밤'을 '상주인구' 칸으로 보고 계산했습니다"
    )


def test_item_whole_uses_cell_named_in_table_name() -> None:
    item = d.Dim(0, "항목", {"T000": "통근·통학 인구(12세 이상)", "T101": "걸어서"})
    nm = "성별, 연령 및 이용 교통수단별 통근 통학 인구(12세 이상)"
    assert d._item_whole(item, nm) == "T000"
    assert d._item_whole(ITEM, "성별/연령별 상주(야간) · 주간 인구-시군구") is None
    assert d._item_whole(SEX, nm) is None  # 항목 분류만


def test_rewrite_terms() -> None:
    parsed = {"term": "자가용 이용 비율", "indicator": "자가용 비율", "year": 2010}
    out = d._rewrite_terms(parsed, [("자가용", "승용차 소형 승합차")])
    assert out["term"] == "승용차 소형 승합차 이용 비율"
    assert out["indicator"] == "승용차 소형 승합차 비율" and parsed["term"] == "자가용 이용 비율"


@pytest.mark.parametrize(
    ("text", "question"),
    [
        (
            '{"pairs": [{"word": "반려견 키우는 가구", "cell": "가구-계"}]}',
            "서울 반려견 키우는 가구 수",
        ),
        ('{"pairs": [{"word": "주간 인구 수", "cell": "주간 인구"}]}', "중구 주간 인구 수"),
    ],
)
def test_pairs_rejects_total_cells_and_added_conditions(text: str, question: str) -> None:
    names = NAMES | {"가구-계"}
    assert cw._pairs(text, question, names) == []
