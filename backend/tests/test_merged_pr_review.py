"""머지된 PR의 잘못된 응답과 지표 연결을 회귀 검증한다. 통계 원자료는 필요 없다."""

from datetime import date

import pytest

from app.services import ask_session, indicators, lookup, qa


@pytest.fixture(autouse=True)
def no_stats_db(monkeypatch):
    monkeypatch.setattr(qa, "_full_region_names", lambda *a: None)
    monkeypatch.setattr(qa, "_ground_regions", lambda *a: None)
    monkeypatch.setattr(indicators, "find_regions", lambda *a: [])
    monkeypatch.setattr(indicators, "_data_years", lambda: (1990, 2025))
    monkeypatch.setattr(indicators, "latest_year", lambda *a: 2025)
    monkeypatch.setattr(indicators, "kosis_url", lambda *a: None)
    monkeypatch.setattr(lookup, "refresh_if_reloaded", lambda: None)
    monkeypatch.setattr(qa, "_lookup_span", lambda *a: (2000, 2025))


def correct(question, indicator, kind="value", regions=None):
    return qa.correct(
        question,
        {"type": kind, "indicator": indicator, "regions": regions or [], "sex": "계"},
        date(2026, 11, 21),
    )


@pytest.mark.parametrize(
    "question", ["누가 제일 예뻐?", "가장 예쁜 사람은 누구?", "누가 가장 이쁘니?"]
)
def test_appearance_questions_have_no_fixed_chat_reply(question):
    assert ask_session.chat_reply(question) is None


@pytest.mark.parametrize(
    "question",
    ["전남은 노인 한 명을 몇 명이 부양해", "전남 노인 1명당 몇 명", "몇 명이 노인 한 명을 부양해"],
)
def test_support_question_uses_people_per_elderly_person(monkeypatch, question):
    parsed = correct(question, "노년부양비", regions=["전남"])
    assert parsed["indicator"] == "노인 1명당 생산연령인구"
    assert parsed["grounded"] and not parsed.get("unsupported")
    name, spec = indicators._get_indicator(parsed["indicator"])
    reg = {"region_code": "46", "display": "전남"}
    monkeypatch.setattr(indicators, "_prepare", lambda *a: (name, spec, [reg], None))
    monkeypatch.setattr(indicators, "_check", lambda *a: None)
    monkeypatch.setattr(
        indicators, "_components", lambda *a: {(2025, "46"): {"old": 200, "working": 500}}
    )
    monkeypatch.setattr(indicators, "_mark_monthly", lambda result, *a: result)
    result = qa.compute(parsed)
    assert result["status"] == "ok"
    assert result["value"] == 2.5
    assert "실제 부양자 수가 아닌" in qa.summary(result)
    assert indicators.INDICATORS["노년부양비"]["calc"]({"old": 200, "working": 500}) == 40


def test_support_ratio_handles_zero_denominator():
    calc = indicators.INDICATORS["노인 1명당 생산연령인구"]["calc"]
    assert calc({"old": 0, "working": 500}) is None
    assert calc({"old": 200, "working": 0}) == 0


@pytest.mark.parametrize("kind", ["value", "rank", "composition"])
def test_birth_share_question_ranks_percentages(monkeypatch, kind):
    parsed = correct("다자녀 출산 비중 높은 지역", "출산순위별 출생 구성", kind=kind)
    assert parsed["indicator"] == "셋째 이상 출생아 비중"
    assert parsed["type"] == "rank"
    spec = indicators.spec(parsed["indicator"])
    src = spec["sources"][0]
    monkeypatch.setattr(lookup, "_pick", lambda *a: (src, None))
    monkeypatch.setattr(
        lookup,
        "_rows",
        lambda *a: [
            {"region_code": "11", "region_name": "서울특별시", "kind": "시도"},
            {"region_code": "26", "region_name": "부산광역시", "kind": "시도"},
        ],
    )
    monkeypatch.setattr(
        lookup, "_table", lambda *a: {"tbl_nm": "출산순위별 출생", "org_nm": "통계", "basis": ""}
    )
    calls = []

    def summed(source, codes, y0, y1, item_key, filters, age_codes):
        calls.append(filters)
        assert filters["c2_id"] == "00"
        if item_key == "item_id":
            assert filters["c3_id"] == ["03", "04", "05", "06", "07", "08"]
            return {(2025, "11"): 15, (2025, "26"): 12}
        assert filters["c3_id"] == "00"
        return {(2025, "11"): 100, (2025, "26"): 40}

    monkeypatch.setattr(lookup, "_sum", summed)
    result = qa.compute(parsed)
    assert result["status"] == "ok"
    assert [r["value"] for r in result["ranking"]] == [30, 15]
    assert result["ranking"][0]["region"].startswith("부산")
    assert len(calls) == 2
    assert "가구의 비중이 아닙니다" in result["answer_note"]


def test_birth_count_composition_and_households_stay_distinct():
    def names(question):
        return {name for *_, name in indicators.find_in_text(question)}

    assert names("다자녀 출산 몇 명") == {"셋째 이상 출생아 수"}
    assert names("출산순위별 출생 구성") == {"출산순위별 출생 구성"}
    assert "셋째 이상 출생아 비중" not in names("다자녀 가구 비중")
    assert names("노년부양비") == {"노년부양비"}
