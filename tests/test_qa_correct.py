from datetime import date

import pytest

from app.services import indicators, qa

# config/catalog_notes.toml 지표 사전 일부 (DB 없이 테스트)
ALIASES = {
    "총인구": ["인구", "인구수", "전체 인구", "주민등록인구"],
    "고령인구 비율": ["고령화율", "노인 비율", "65세 이상 비율"],
    "유소년인구 비율": ["유소년 비율", "0~14세 비율"],
    "노령화지수": ["노령화 지수"],
    "합계출산율": ["출산율", "TFR"],
    "순이동": ["인구 순유입"],
}
TODAY = date(2026, 10, 24)


@pytest.fixture(autouse=True)
def fixed_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_alias_map", lambda: ALIASES)


def parsed(**kw) -> dict:
    base = {"type": "value", "indicator": None, "regions": [], "year": None, "sex": "계"}
    return {**base, **kw}


def test_indicator_alias_overrides_llm() -> None:
    # HCX가 "고령화율"을 비슷한 이름의 노령화지수로 해석한 경우
    p = qa.correct(
        "최근 5년간 부산 고령화율 변화는?",
        parsed(type="trend", indicator="노령화지수", regions=["부산"]),
        TODAY,
    )
    assert p["indicator"] == "고령인구 비율"
    assert "corrections" in p


def test_indicator_phrase_with_change_word() -> None:
    # "인구 증감률"을 통째로 지표 이름으로 해석한 경우
    p = qa.correct(
        "2015~2025년 대구 인구 증감률은?",
        parsed(type="trend", indicator="인구 증감률", regions=["대구"]),
        TODAY,
    )
    assert p["indicator"] == "총인구"


def test_female_elderly_ratio() -> None:
    p = qa.correct(
        "작년 부산 여성 노인 비율은?",
        parsed(indicator="여성 노인 비율", regions=["부산"], year=2023),
        TODAY,
    )
    assert p["indicator"] == "고령인구 비율"
    assert p["sex"] == "여"
    assert p["year"] == 2025


def test_nationwide_region() -> None:
    p = qa.correct(
        "2009년부터 2016년까지 전국 인구 변화는?",
        parsed(type="trend", indicator="총인구", regions=[]),
        TODAY,
    )
    assert p["regions"] == ["전국"]


def test_no_change_when_llm_is_right() -> None:
    p = qa.correct(
        "2025년 서울 고령인구 비율은?",
        parsed(indicator="고령인구 비율", regions=["서울"], year=2025),
        TODAY,
    )
    assert "corrections" not in p


def test_longer_alias_wins() -> None:
    # "유소년인구 비율" 안의 "인구"는 총인구로 세지 않는다
    assert {n for *_, n in indicators.find_in_text("세종 유소년인구 비율 비교")} == {
        "유소년인구 비율"
    }
    # 조회 지표 별칭 안의 "인구"도 마찬가지
    assert {n for *_, n in indicators.find_in_text("서울 인구 순유입은?")} == {"순이동"}


def test_unsupported_lookup_indicator_is_named() -> None:
    # 조회 지표는 아직 답하지 못하지만, 엉뚱한 계산 지표로 바뀌지 않고 그 이름으로 안내된다
    p = qa.correct("2024년 서울 출산율은?", parsed(indicator="총인구", regions=["서울"]), TODAY)
    assert p["indicator"] == "합계출산율"


@pytest.mark.parametrize(
    "question",
    [
        "2025년 서울 20대 인구는?",  # 연령 조건이 있으면 총인구로 바꾸지 않는다
        "서울 고령화율과 노령화지수를 비교해줘",  # 지표가 둘이면 HCX 해석을 따른다
        "저출산 정책이 효과가 있었나요?",  # 지표 표현이 없으면 HCX 해석을 따른다
    ],
)
def test_keeps_llm_indicator(question: str) -> None:
    p = qa.correct(question, parsed(indicator="HCX 해석"), TODAY)
    assert p["indicator"] == "HCX 해석"


def test_both_genders_or_rank_untouched() -> None:
    p = qa.correct(
        "2025년 전국 시도 중 남성과 여성 인구 차이가 큰 곳은?",
        parsed(type="rank", indicator="총인구"),
        TODAY,
    )
    assert p["sex"] == "계"
    assert p["regions"] == []


def test_relative_years() -> None:
    assert qa.correct("올해 서울 인구는?", parsed(indicator="총인구"), TODAY)["year"] == 2026
    assert qa.correct("재작년 서울 인구는?", parsed(indicator="총인구"), TODAY)["year"] == 2024
    # 추이 질문의 기간은 건드리지 않는다
    p = qa.correct("작년까지 5년간 변화", parsed(type="trend", indicator="총인구"), TODAY)
    assert p["year"] is None
