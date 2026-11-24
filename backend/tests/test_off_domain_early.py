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
        "서울 아파트 평균 가격은?",
        "강남 아파트 시세 알려줘",
    ],
)
def test_refuses_topics_outside_population_without_llm(question: str) -> None:
    res = early(question)
    assert res is not None and res["status"] == "out_of_scope"
    assert res["tables"]["status"] == "out_of_scope"
    assert "LLM 호출 전" in res["tables"]["reason"]


def test_word_found_in_catalog_is_not_refused() -> None:
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
    res = early(question)
    assert res is not None and res["status"] == "out_of_scope"
    assert f"'{outside}'" in res["answer"] and "인구 분야 통계가 아니라" in res["answer"]
    assert "출생아 수" in res["answer"]


def test_coordinated_words_finds_the_asked_phrase() -> None:
    assert table_guide.coordinated_words("출생아 수랑 아파트 매매가격", ["매매가"]) == {
        "매매가": "아파트 매매가격"
    }
    assert table_guide.coordinated_words("아파트 매매가격이랑 출생아 수", ["매매가"]) == {
        "매매가": "아파트 매매가격"
    }
    assert table_guide.coordinated_words("물가 비싼 서울에 사는 1인 가구", ["물가"]) == {}
    q = "2020년 서울과 집값이 비싼 부산의 1인 가구 수는?"
    assert table_guide.coordinated_words(q, ["집값"]) == {}
    assert table_guide.coordinated_words("서울과 집값 오른 지역의 인구", ["집값"]) == {}
    assert table_guide.coordinated_words("출생아 수랑 집값이 궁금해", ["집값"]) == {"집값": "집값"}


def test_catalog_check_uses_the_word_the_user_wrote(catalog) -> None:
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
    from app.services import indicators

    monkeypatch.setattr(indicators, "find_regions", lambda text: ["부산"] if "부산" in text else [])
    res = early("부산 청년 실업률 알려줘")
    assert res is not None and res["status"] == "out_of_scope"
    assert "'실업률'은" in res["answer"] and "'청년 인구'는 볼 수 있습니다" in res["answer"]
    assert "예) 부산 청년 인구는?" in res["answer"]
    assert "인구 · 가구 · 출생" in early("작년 GDP 얼마야")["answer"]
