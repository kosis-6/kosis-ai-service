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


@pytest.mark.parametrize(
    ("question", "outside"),
    [
        ("2024년 부산 출생아 수랑 아파트 매매가격 알려줘", "아파트 매매가격"),
        ("서울 집값과 출생아 수 알려줘", "서울 집값"),
        ("2024년 서울 집값과 부산 출생아 수 알려줘", "서울 집값"),
        ("서울 집값,부산 출생아 수 알려줘", "서울 집값"),
        ("2024년 부산 출생아 수, 실업률 알려줘", "실업률"),
    ],
)
def test_off_domain_indicator_asked_with_population_refuses_whole(question, outside) -> None:
    # 출생아 수만 답하면 다른 질문의 답이 된다 (전에는 매매가격을 조용히 빼고 출생아 수만 답했다)
    res = early(question)
    assert res is not None and res["status"] == "out_of_scope"
    assert f"'{outside}'" in res["answer"] and "인구 분야 통계가 아니라" in res["answer"]
    assert "출생아 수" in res["answer"]  # 나머지 지표는 따로 물으면 볼 수 있다고 알린다


def test_coordinated_words_finds_the_asked_phrase() -> None:
    assert table_guide.coordinated_words("출생아 수랑 아파트 매매가격", ["매매가"]) == {
        "매매가": "아파트 매매가격"
    }
    assert table_guide.coordinated_words("아파트 매매가격이랑 출생아 수", ["매매가"]) == {
        "매매가": "아파트 매매가격"
    }
    # 꾸밈말은 나란히 물은 지표가 아니다
    assert table_guide.coordinated_words("물가 비싼 서울에 사는 1인 가구", ["물가"]) == {}
    # '과' 뒤에 와도 뒤의 지역을 꾸미면 따로 물은 지표가 아니다 (거절했었다)
    q = "2020년 서울과 집값이 비싼 부산의 1인 가구 수는?"
    assert table_guide.coordinated_words(q, ["집값"]) == {}
    assert table_guide.coordinated_words("서울과 집값 오른 지역의 인구", ["집값"]) == {}
    # 주어로 쓰여도 꾸밈이 아니면 물은 지표다
    assert table_guide.coordinated_words("출생아 수랑 집값이 궁금해", ["집값"]) == {"집값": "집값"}


def test_catalog_check_uses_the_word_the_user_wrote(catalog) -> None:
    # '전세가'는 인구 표의 '전세가구' 안에 있지만 물은 말은 '전세가격'이다
    # (출생아 수만 답하고 전세가격을 조용히 뺐다)
    catalog.append(_table("점유형태별 전세가구 월세가구"))
    res = early("부산 출생아 수와 전세가격 같이 알려줘")
    assert res is not None and res["status"] == "out_of_scope"
    assert "'전세가격'" in res["answer"]
    assert table_guide.asked_word("전세가격 알려줘", "전세가") == "전세가격"
    assert table_guide.asked_word("집값이 비싼", "집값") == "집값"


@pytest.mark.parametrize(
    "question",
    [
        "2020년 서울과 집값이 비싼 부산의 1인 가구 수는?",
        "2020년 서울과 집값이 정말 비싼 부산의 1인 가구 수는?",
        "2020년 서울과 집값이 예전보다 많이 오른 부산의 1인 가구 수는?",
        "2020년 서울과 집값이 비싸지 않은 부산의 1인 가구 수는?",
    ],
)
def test_off_domain_modifier_after_conjunction_is_not_refused(question: str) -> None:
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
