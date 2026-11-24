"""답변 불가 사유 (app/services/refusal.py) · 그 사유를 내는 판정들"""

import asyncio

import pytest

from app.services import indicators, qa, question_clarify, refusal

# ---- LLM 전에 알 수 있는 '통계 질문이 아님' --------------------------------------------------


@pytest.mark.parametrize(
    ("question", "status"),
    [
        ("혼인신고 어디서 해요?", "not_statistics"),
        ("사망신고는 어떻게 하나요", "not_statistics"),
        ("주민등록등본 발급 방법 알려줘", "not_statistics"),
        ("신혼부부가 살기 좋은 도시 추천해 줘", "not_statistics"),
        ("서울 살기 어때?", "not_statistics"),
        ("이사 가려는데 세종 어때?", "not_statistics"),
        ("이사하려는데 대전 어때?", "not_statistics"),
        ("은퇴하고 이주할 곳으로 제주 어때?", "not_statistics"),
        ("결혼하기 좋은 도시 어때?", "not_statistics"),
        ("서울이랑 부산 중 어디가 살기 좋아?", "not_statistics"),
        ("어디가 더 나을까?", "not_statistics"),
        ("이 챗봇은 뭘 할 수 있나요?", "chat"),
        ("사용법 좀 알려줘", "chat"),
    ],
)
def test_early_catches_non_statistics(question: str, status: str) -> None:
    hit = refusal.early(question)
    assert hit is not None and hit[0] == status
    assert "예)" in hit[1]  # 대신 물어볼 수 있는 질문 예시를 준다


@pytest.mark.parametrize(
    "question",
    [
        "작년 혼인신고 건수 몇 건이야?",  # 수치를 묻는다
        "출생신고 통계 어디서 봐?",
        "2024년 부산 출생아 수",
        "2024년 부산 합계출산율 어때?",
        "부산은 어때?",  # 앞 질문의 지역을 바꾸는 말일 수 있다
        "요즘 출산율 어때?",
        "2023년 서울 이주배경인구 어때?",  # 지표 이름 속 '이주'는 의견 말이 아니다
        "외국인 이주민 어때?",
        "대전 인구 추이 알려줘",
        "전입 인구가 많은 시도 순위",
    ],
)
def test_early_leaves_statistics_questions(question: str) -> None:
    assert refusal.early(question) is None


# ---- 끝에서 사유 고치기 ---------------------------------------------------------------------


def _finalize(res: dict, question: str = "질문") -> dict:
    asyncio.run(refusal.finalize(res, question))
    return res


def test_sub_district_is_a_region_level_gap() -> None:
    res = {"status": "unknown_region", "result": {"status": "unknown_region", "region": "역삼동"}}
    out = _finalize(res)
    assert out["status"] == "no_region_level" and "읍면동" in out["answer"]
    assert out["trace"]["refusal"]["from"] == "unknown_region"


def test_unknown_region_that_is_not_a_district_stays() -> None:
    res = {"status": "unknown_region", "result": {"status": "unknown_region", "region": "아틀란"}}
    assert _finalize(res)["status"] == "unknown_region"


def _guide(checks: dict, status: str = "partial_match") -> dict:
    rec = {"tbl_id": "T1", "tbl_nm": "연령별 표", "checks": checks}
    return {
        "status": "tables",
        "answer": "통계표 안내",
        "result": {"status": "tables"},
        "tables": {"status": status, "recommendations": [rec], "notice": "조건 ✘"},
    }


def test_table_guide_with_only_age_range_unmet_is_band_mismatch() -> None:
    out = _finalize(_guide({"region_level": True, "class:연령": True, "item:41살에서 46살": False}))
    assert out["status"] == "band_mismatch" and "연령 구간" in out["answer"]


def test_table_guide_with_other_condition_unmet_is_partial_match() -> None:
    out = _finalize(_guide({"region_level": True, "item:직업": False}))
    assert out["status"] == "partial_match" and out["answer"] == "통계표 안내"


def test_table_guide_with_all_conditions_met_stays_tables() -> None:
    out = _finalize(_guide({"region_level": True}, status="ok"))
    assert out["status"] == "tables"


def test_answer_and_clarification_are_untouched() -> None:
    assert _finalize({"status": "ok"})["status"] == "ok"
    asking = {"status": "tables", "clarification": {"kind": "x"}, "tables": {}}
    assert _finalize(asking)["status"] == "tables"


def test_review_refusal_is_classified(monkeypatch) -> None:
    seen = {}

    async def fake(question, reason):
        seen["reason"] = reason
        return {"code": "no_joint_table", "detail": "두 구분을 함께 센 표가 없습니다.", "ms": 1}

    monkeypatch.setattr(refusal, "classify", fake)
    res = {
        "status": "out_of_scope",
        "answer": "범위 밖",
        "tables": {"status": "out_of_scope", "reason": f"{refusal.REVIEW_NONE} (A 표에 B 없음)"},
    }
    out = _finalize(res)
    # 분류기가 쓴 설명은 답에 넣지 않는다 (사유마다 정해 둔 문장만). 기록에는 남긴다
    assert out["status"] == "no_joint_table"
    assert out["answer"] == refusal.TEMPLATES["no_joint_table"]
    assert out["trace"]["refusal_reason"]["detail"] == "두 구분을 함께 센 표가 없습니다."
    assert seen["reason"] == "A 표에 B 없음"


def test_word_check_refusal_is_not_classified(monkeypatch) -> None:
    async def fail(*_):
        raise AssertionError("단어 검사로 거절한 질문은 분류하지 않는다")

    monkeypatch.setattr(refusal, "classify", fail)
    res = {
        "status": "out_of_scope",
        "tables": {"status": "out_of_scope", "reason": "'물가'은 인구 통계 범위 밖입니다"},
    }
    assert _finalize(res)["status"] == "out_of_scope"


def test_classify_failure_keeps_out_of_scope(monkeypatch) -> None:
    async def none(*_):
        return None

    monkeypatch.setattr(refusal, "classify", none)
    res = {
        "status": "out_of_scope",
        "tables": {"status": "out_of_scope", "reason": f"{refusal.REVIEW_NONE} (이유)"},
    }
    assert _finalize(res)["status"] == "out_of_scope"


# ---- 사유를 내는 판정 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("2045년에 인구가 얼마나 될까", True),
        ("출산율이 반등할 것 같아?", True),
        ("내후년에 혼인이 더 늘까요", True),
        ("지금 서울 인구는 몇 명일까", False),  # 지금 값을 묻는다
        ("부산 인구 알려줘", False),
    ],
)
def test_asks_guess(question: str, expected: bool) -> None:
    assert qa.asks_guess(question) is expected


def test_gestation_weeks_is_an_unsupported_condition() -> None:
    assert qa.UNSUPPORTED_MEASURE.search("임신 37주 미만에 태어난 아기")
    assert qa.UNSUPPORTED_MEASURE.search("28주에서 31주 사이 출생아")
    assert not qa.UNSUPPORTED_MEASURE.search("최근 5년 출생아 수")


def test_region_not_yet_founded_comes_before_data_range() -> None:
    reg = {"region_name": "세종특별자치시", "kind": "시도", "region_code": "36"}
    bad = indicators.not_yet_founded(reg, 1995, "출생아 수")
    assert bad["status"] == "region_not_in_year" and bad["final"] and "2012년" in bad["reason"]
    assert indicators.not_yet_founded(reg, 2015, "출생아 수") is None
    # 통합 · 이름 바꿈으로 코드만 새로 생긴 지역은 해당하지 않는다
    assert indicators.founded_year({"region_name": "창원시(통합)", "valid_from": 2010}) is None


@pytest.mark.parametrize(
    "question",
    ["여기 출생아 수 알려줘", "이 동네 고령 인구 몇 명이야", "특정 시군구의 사망자 수는?"],
)
def test_vague_region_words(question: str) -> None:
    assert question_clarify.detect(question, [])["kind"] == "local_region"


def test_here_inside_a_word_is_not_a_vague_region() -> None:
    assert question_clarify.detect("서울 인구 여기저기 흩어진 통계", []) is None


def test_trend_for_every_sigungu_asks_which_one() -> None:
    q = "2020년부터 시군구별 고령 인구 비중은 어떻게 변했어?"
    asked = question_clarify.detect(q, [])
    assert asked["kind"] == "local_region" and "어느 시군구" in asked["prompt"]
    merged = question_clarify.merge_region(q, "전주시")
    assert merged == "2020년부터 전주시 고령 인구 비중은 어떻게 변했어?"


@pytest.mark.parametrize(
    "question",
    [
        "시군구별 합계출산율이 가장 높은 곳은?",  # 순위: 답이 몇 곳으로 줄어든다
        "최근 5년 인구가 10% 이상 줄어든 시군구를 시군구별 추이로 찾아줘",  # 조건으로 거른다
        "시도별 출생아 수 추이",  # 17곳은 보여 줄 수 있다
        "시군구별 인구 알려줘",  # 추이가 아니다
    ],
)
def test_narrowed_or_small_breakdowns_are_not_asked(question: str) -> None:
    assert question_clarify.detect(question, []) is None


def test_named_region_is_not_asked_again() -> None:
    q = "시군구별 인구 추이, 대전 유성구는 어떻게 변했어?"
    assert question_clarify.detect(q, ["대전광역시 유성구"]) is None


@pytest.mark.needs_data  # 지역 이름 목록(regions · region_aliases)을 읽는다 (CI에는 없음)
def test_age_decade_is_not_read_as_a_region() -> None:
    # "30대 전체"의 '대전'을 지역으로 읽으면 묻지 않은 대전광역시 값으로 답한다
    assert "대전" not in indicators.find_regions("40대 전체 인구 중 미혼 비중")
    assert indicators.find_regions("2024년 대전 인구") == ["대전"]


def test_whole_country_word_does_not_count_as_a_named_region() -> None:
    q = "시군구별 40대 전체 인구 중 기혼 비중은 어떻게 변했어?"
    assert question_clarify.detect(q, ["전체"])["kind"] == "local_region"


def _multi(status: str, outside: list[str]) -> dict:
    targets = [{"span": "평균 초혼 연령"}, {"span": "조출생률"}]
    handoff = {"targets": targets, "combine": {"op": "filter_then_lookup", "out_of_scope": outside}}
    return {
        "status": status,
        "answer": "원래 답",
        "result": {"status": status},
        "tables": {"status": "ok", "recommendations": [], "handoff": handoff},
    }


@pytest.mark.parametrize("status", ["tables", "ok"])
def test_one_indicator_outside_the_domain_refuses_the_whole_question(status, monkeypatch) -> None:
    monkeypatch.setattr(indicators, "find_regions", lambda t: [])  # 지역 이름 목록(DB) 대신
    out = _finalize(_multi(status, ["전세 가격 상승률"]))
    assert out["status"] == "out_of_scope"
    assert "'전세 가격 상승률'" in out["answer"] and "평균 초혼 연령 · 조출생률" in out["answer"]


def test_outside_text_uses_particle_for_the_word() -> None:
    assert refusal._outside_text(["미세먼지"], ["사망자 수"]).startswith("'미세먼지'는 ")
    assert refusal._outside_text(["집값"], []).startswith("'집값'은 ")
    assert refusal._outside_text(["집값", "물가"], []).startswith("'집값' · '물가' 등은 ")


def test_region_name_listed_as_outside_indicator_is_not_refused(monkeypatch) -> None:
    # 계획이 지역 이름("부산")을 범위 밖 지표로 적어도 거절하지 않는다
    monkeypatch.setattr(indicators, "find_regions", lambda t: ["부산"] if "부산" in t else [])
    assert _finalize(_multi("tables", ["부산"]))["status"] == "tables"
    assert _finalize(_multi("tables", ["부산광역시"]))["status"] == "tables"
    out = _finalize(_multi("tables", ["부산", "부산 집값"]))  # 지역이 붙은 다른 지표는 그대로
    assert out["status"] == "out_of_scope"
    assert "'부산 집값'" in out["answer"] and "'부산'" not in out["answer"]


def test_all_indicators_inside_the_domain_are_left_alone() -> None:
    assert _finalize(_multi("ok", []))["status"] == "ok"
    assert _finalize(_multi("tables", []))["status"] == "tables"
