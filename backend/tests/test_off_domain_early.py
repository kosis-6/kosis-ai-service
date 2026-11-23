"""범위 밖 질문을 LLM 호출 전에 거르기 (table_guide.off_domain_early, 11/18)"""

import asyncio

import pytest

from app.services import ask_session, qa, search, table_guide


def _table(name: str) -> dict:
    n = search.norm(name)
    return {"name_n": n, "meta_n": "", "path_n": ""}


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    tables = [
        _table("시군구별 주민등록인구"),
        _table("점유형태별 가구 월세"),
        _table("고용률 조사표"),
    ]

    async def load():
        return tables

    monkeypatch.setattr(search, "load_tables", load)
    return tables


def early(question: str):
    return asyncio.run(table_guide.off_domain_early(question))


@pytest.mark.parametrize(
    "question",
    [
        "부산 아파트 값 요새 어때?",
        "오늘 대전 미세먼지 심해?",
        "올해 물가 상승률 알려 줘",
        "작년 GDP 얼마야",
    ],
)
def test_refuses_topics_outside_population_without_llm(question: str) -> None:
    res = early(question)
    assert res is not None and res["status"] == "out_of_scope"
    assert res["tables"]["status"] == "out_of_scope"
    assert "LLM 호출 전" in res["tables"]["reason"]


def test_word_found_in_catalog_is_not_refused() -> None:
    # 안전장치 1: 카탈로그(표 이름 · 분류 · 항목)에 있는 말이면 거절하지 않는다
    assert early("청소년 고용률 몇 퍼센트야") is None


@pytest.mark.parametrize(
    "question",
    [
        "물가 비싼 서울에 사는 1인 가구 몇 개야?",
        "미세먼지 심한 동네 인구 알려 줘",
        "날씨 좋은 제주로 이사 온 사람",
    ],
)
def test_population_signal_is_not_refused(question: str) -> None:
    # 안전장치 2: 인구 통계를 묻는 질문이면 범위 밖 말이 있어도 지금처럼 뒤 단계로
    assert early(question) is None


def test_plain_population_question_is_untouched() -> None:
    assert early("강남구 사람 몇 명이야?") is None


def test_new_question_skips_understanding(monkeypatch) -> None:
    async def never(*args, **kwargs):
        raise AssertionError("질문 이해(HCX)를 부르면 안 된다")

    monkeypatch.setattr(qa, "ask", never)
    res, sources, notice = asyncio.run(ask_session._new("서울 집값 요즘 얼마야", None))
    assert res["status"] == "out_of_scope" and res["request_type"] == ask_session.NEW
    assert sources == {} and notice is None


def test_modifier_of_off_domain_word_is_not_population_signal(monkeypatch) -> None:
    # "청년 실업률"의 청년은 실업률을 꾸밀 뿐: 인구 신호로 보지 않고 거절한다 (11/23).
    # 전에는 지역을 되물은 뒤에야 "통계표를 찾지 못했습니다"로 끝났다
    from app.services import indicators

    monkeypatch.setattr(indicators, "find_regions", lambda text: ["부산"] if "부산" in text else [])
    res = early("부산 청년 실업률 알려줘")
    assert res is not None and res["status"] == "out_of_scope"
    # 무엇이 범위 밖인지와 대신 볼 수 있는 인구 통계를 알린다
    assert "'실업률'은" in res["answer"] and "'청년 인구'는 볼 수 있습니다" in res["answer"]
    assert "예) 부산 청년 인구는?" in res["answer"]
    # 인구 집단 말이 없으면 볼 수 있는 분야만
    assert "인구 · 가구 · 출생" in early("작년 GDP 얼마야")["answer"]
