"""자동 채점 규칙(scripts/devset_grade.py): 챗봇·HCX 없이 가짜 문항과 가짜 응답으로 확인한다
(실제 골든셋 문항·정답은 쓰지 않는다)"""

from scripts import devset_grade as dg

SRC = {
    "name": "국가데이터처, 가상 인구표 (DT_TEST01)",
    "basis": "2025년 12월 31일 기준",
    "tbl_id": "DT_TEST01",
    "url": "https://kosis.kr/statHtml/statHtml.do?orgId=101&tblId=DT_TEST01",
}


def row(**kw):
    base = {
        "문항 ID": "D-900",
        "질문": "가상시 인구 몇 명?",
        "기대 행동": "답변",
        "질문 종류": "단순 수치",
        "정답 표": "가상 인구표 (DT_TEST01)\n허용: 가상 인구표2 (DT_TEST02)",
        "정답 조건": "가상시, 2025, 주민등록인구, 계",
        "적용 기본값": "",
        "정답 수치": "123,456명",
        "데이터 경로": "적재",
        "필요 표 수": "단일",
        "사용자 답": "",
        "답변 불가 유형": "",
        "정답 확인 근거": "",
    }
    base.update(kw)
    return base


def res(answer, status="ok", result=None, regions=("가상시",), year=2025, **kw):
    base = {
        "status": status,
        "answer": answer,
        "parsed": {"regions": list(regions), "year": year, "sex": "계"},
        "result": result if result is not None else {"value": 123456},
        "sources": [SRC],
        "applied": [],
    }
    base.update(kw)
    return base


# ---- 숫자 ---------------------------------------------------------------------------------


def test_rounding_and_unit_conversion_match():
    exp = dg.numbers("20.62%")[0]
    assert dg.same_number(exp, dg.numbers("20.6%")[0])
    assert not dg.same_number(exp, dg.numbers("20.4%")[0])
    assert dg.same_number(dg.numbers("52,000명")[0], dg.numbers("5.2만 명")[0])
    # 단위 설명의 "1천 명당"은 환산하지 않는다
    assert dg.numbers("인구 1천 명당 4.7건")[0].value == 1


def test_coarser_rounding_in_answer_matches():
    """답이 자릿수를 줄여 적거나 만 단위로 적은 것은 같은 수"""
    assert dg.same_number(dg.numbers("0.107")[0], dg.numbers("0.11")[0])
    assert dg.same_number(dg.numbers("1,604,920명")[0], dg.numbers("160만 명")[0])
    assert dg.same_number(dg.numbers("18,145명")[0], dg.numbers("18,145명")[0])


def test_unitless_integer_in_answer_does_not_match_decimal():
    """출처 · 단위 문구의 정수가 소수 정답과 같은 수로 잡히지 않는다"""
    assert not dg.same_number(dg.numbers("1.17배")[0], dg.numbers("가임여성 1명당")[0])
    assert not dg.same_number(dg.numbers("1.39배")[0], dg.numbers("1세별 주민등록인구")[0])
    assert not dg.same_number(dg.numbers("5.2명")[0], dg.numbers("연령(5세)별 이동자수")[0])
    assert not dg.same_number(dg.numbers("0.799")[0], dg.numbers("1월 1일")[0])


def test_ratio_not_found_from_source_text():
    """비교 정답의 배수가 답에 없으면 출처 문구의 1로 채우지 않는다"""
    exp = dg.expected_numbers("비교", "서울 0.632명, 부산 0.739명. 차이 +0.107명 (부산이 1.17배)")
    answer = "부산이 0.107명 높습니다. ※ 가임여성 1명당 명 · 각 연도 1월 1일 기준"
    nums = dg.numbers(answer)
    found = {e.label: dg.find_number(e, nums, answer) for e in exp}
    assert found == {"차이": True, "배수": False}


def test_expected_numbers_by_kind():
    trend = dg.expected_numbers(
        "추이", "2021년 1명 → 2025년 2명. 증감량 +1명, 증감률 +100.0%, 연평균 +18.9%"
    )
    assert [e.label for e in trend] == ["증감량", "증감률", "연평균"]
    cmp = dg.expected_numbers("비교", "A 28.5%, B 20.4%. 차이 8.0%p, 1.40배")
    assert [(e.label, e.num.value, e.percent_point) for e in cmp] == [
        ("차이", 8.0, True),
        ("배수", 1.4, False),
    ]
    agg = dg.expected_numbers("집계", "합계 300명 (A 100명 + B 200명)\n참고 줄")
    assert [e.num.value for e in agg] == [300]
    share = dg.expected_numbers("구성비", "21.2% (65세 이상 1,000명 / 총인구 4,717명)")
    assert [(e.label, e.num.value) for e in share] == [
        ("비율", 21.2),
        ("분자", 1000),
        ("분모", 4717),
    ]
    rank = dg.expected_numbers("순위", "1위 가상도 3.0명; 2위 나상도 2.0명. 전국 1.0명")
    assert [e.label for e in rank] == ["1위 가상도", "2위 나상도"]


def test_percent_point_required_for_rate_gap():
    r = row(질문종류="비교")
    r["질문 종류"] = "비교"
    r["정답 수치"] = "A 28.5%, B 20.4%. 차이 8.0%p, 1.40배"
    with_pp = dg.grade(
        r, res("A 28.5%, B 20.4%로 8.0%p 높고 1.4배입니다.", result={"v": [28.5, 20.4, 8.1]})
    )
    without = dg.grade(r, res("A 28.5%, B 20.4%로 8.0% 높고 1.4배입니다.", result={"v": [28.5]}))
    assert with_pp.number == "O"
    assert without.number == "X"


def test_rank_order():
    assert dg.ranks_in_order(["전라남도", "세종특별자치시"], "1위 전남 1.09, 2위 세종 1.06")
    assert not dg.ranks_in_order(["전라남도", "세종특별자치시"], "1위 세종 1.06, 2위 전남 1.09")


# ---- 한 문항 ---------------------------------------------------------------------------------


def test_correct_answer():
    g = dg.grade(row(), res("가상시 인구는 123,456명입니다."))
    assert (g.actual, g.number, g.condition, g.source, g.correct) == ("답변", "O", "O", "O", "O")
    assert g.hallucination == 0


def test_allowed_table_is_accepted():
    other = {**SRC, "tbl_id": "DT_TEST02", "url": SRC["url"].replace("01", "02")}
    g = dg.grade(row(), res("123,456명", sources=[other]))
    assert g.source == "O"


def test_wrong_table_reason_is_table():
    other = {**SRC, "tbl_id": "DT_OTHER9", "url": SRC["url"].replace("TEST01", "OTHER9")}
    g = dg.grade(row(), res("99명", result={"value": 99}, sources=[other]))
    assert (g.source, g.correct, g.reason) == ("X", "X", "표")


def test_missing_source_elements():
    g = dg.grade(row(), res("123,456명", sources=[{**SRC, "basis": "", "url": ""}]))
    assert g.source == "X"
    assert set(g.details["빠진 출처"]) == {"기준 시점", "링크"}


def test_wrong_period_reason_is_condition():
    g = dg.grade(row(), res("123,456명", year=2024))
    assert g.condition == "X"
    assert g.reason == "조건"


def test_number_in_result_but_not_in_answer_is_summary():
    g = dg.grade(row(), res("가상시 인구는 120,000명입니다."))
    assert g.number == "X"
    assert g.reason == "요약"
    assert g.hallucination == 1  # 120,000은 계산 결과에 없다


def test_wrong_calculation_is_calculation():
    g = dg.grade(row(), res("가상시 인구는 99명입니다.", result={"value": 99}))
    assert g.reason == "계산"


def test_years_and_rank_labels_are_not_hallucination():
    r = res("2025년 기준 1위 가상시 123,456명입니다.")
    assert dg.hallucinated_numbers(r) == []


def test_claim_sentence_candidates():
    text = "인구는 123,456명입니다. 이는 일자리 정책 때문으로 보입니다."
    assert dg.claim_sentences(text) == ["이는 일자리 정책 때문으로 보입니다."]


# ---- 행동 ---------------------------------------------------------------------------------


def test_clarify_then_final_answer():
    r = row(**{"기대 행동": "되묻기", "사용자 답": "가상시"})
    asked = {
        "status": None,
        "answer": "어느 지역인가요?",
        "clarification": {"kind": "local_region"},
    }
    g = dg.grade(r, asked, res("123,456명"))
    assert (g.actual, g.correct) == ("되묻기", "O")


def test_missed_clarification():
    r = row(**{"기대 행동": "되묻기", "사용자 답": "가상시"})
    g = dg.grade(r, res("123,456명"))
    assert (g.actual, g.correct, g.reason) == ("답변", "X", "행동 (되묻기 누락)")


def test_unneeded_clarification():
    asked = {"status": "missing_region", "answer": "어느 지역인가요?"}
    g = dg.grade(row(), asked)
    assert g.reason == "행동 (불필요한 되묻기)"


def test_refusal_type_and_alternative():
    r = row(**{"기대 행동": "답변 불가", "답변 불가 유형": "없는 연도", "질문 종류": ""})
    good = {
        "status": "out_of_range",
        "answer": "2027년 자료는 없습니다.",
        "result": {"available_years": [2000, 2025]},
    }
    assert dg.grade(r, good).correct == "O"
    no_alt = {"status": "out_of_range", "answer": "자료가 없습니다.", "result": {}}
    assert dg.grade(r, no_alt).reason == "요약 (대안 없음)"
    wrong = {"status": "unknown_indicator", "answer": "지원하지 않는 지표입니다.", "result": {}}
    assert dg.grade(r, wrong).reason == "행동 (불가 유형 오류)"


def test_search_out_of_scope_is_off_topic_refusal():
    r = row(**{"기대 행동": "답변 불가", "답변 불가 유형": "범위 밖 주제", "질문 종류": ""})
    off = {
        "status": "tables",
        "answer": "제공 범위(KOSIS 인구 분야 통계표)에서 질문에 맞는 통계표를 찾지 못했습니다. "
        "찾는 통계의 주제나 표 이름에 들어갈 단어로 다시 질문해 주세요.",
        "tables": {"status": "out_of_scope", "reason": "검토 결과 후보 통계표가 …"},
    }
    g = dg.grade(r, off)
    assert g.correct == "O"
    assert g.details["거절 근거"].startswith("검토 결과")
    # 표를 추천한 답은 범위 밖 거절이 아니다
    rec = {"status": "tables", "answer": "추천 통계표: …", "tables": {"status": "ok"}}
    assert dg.grade(r, rec).reason == "행동 (불가 유형 오류)"


def test_tables_only_answer_counts_as_refusal():
    assert dg.action_of({"status": "tables", "tables": {"recommendations": [1]}}) == "답변 불가"
    assert dg.action_of({"status": "tables", "tables": {"clarify": {"options": []}}}) == "되묻기"


def test_nationwide_question_has_no_region_list():
    r = row(**{"정답 조건": "전국, 2025, 이혼 건수"})
    g = dg.grade(r, res("2025년 전국의 이혼 건수는 123,456건입니다.", regions=()))
    assert g.condition == "O"


def test_period_is_read_from_answer_lead():
    r = row(
        **{
            "질문 종류": "집계",
            "정답 조건": "가상시, 2023~2025, 출생아 수",
            "정답 수치": "합계 300명",
        }
    )
    stale = res("2023~2025년 가상시의 출생아 수는 300명입니다.\n\n표", result={"total": 300})
    stale["parsed"].update({"start_year": 2022, "end_year": 2024})
    assert dg.grade(r, stale).condition == "O"


def test_rank_names_full_or_short():
    assert dg.ranks_in_order(["전라남도", "경상북도"], "전라남도(28.4%), 경상북도(27.4%)")


def test_other_table_accepted_when_numbers_right():
    other = {**SRC, "tbl_id": "DT_OTHER9", "url": SRC["url"].replace("TEST01", "OTHER9")}
    g = dg.grade(row(), res("가상시 인구는 123,456명입니다.", sources=[other]))
    assert (g.source, g.correct) == ("O", "O")


def test_projection_fallback_counts_as_answer():
    r = {"status": "out_of_range", "result": {"suggestion": {"status": "ok"}}}
    assert dg.action_of(r) == "답변"


def test_coverage_notice_in_lead_does_not_widen_period():
    r = row(**{"정답 조건": "전국, 2050, 장래 추계인구", "정답 수치": "47,000명"})
    text = "자료는 2008~2025년만 있습니다. 대신 추계값을 보여드립니다. 2050년 전국 47,000명입니다."
    assert dg.check_conditions(r["정답 조건"], res(text, regions=())) == []
    wrong = "가상시의 인구는 2020년 1명, 2025년 2명입니다."
    assert dg.check_conditions("가상시, 2021~2025, 인구, 계", res(wrong)) != []


def test_single_sex_condition_checks_sex():
    female = "가상시, 2025, 주민등록인구 여자"
    text = "2025년 가상시 남자 인구는 100명입니다. (여자 123,456명)"
    wrong = res(text)
    wrong["parsed"]["sex"] = "남"
    assert dg.check_conditions(female, wrong) == ["성별 남 (정답 여)"]
    right = res("2025년 가상시 여자 인구는 123,456명입니다.")
    right["parsed"]["sex"] = "여"
    assert dg.check_conditions(female, right) == []
    # 아내·남편도 성별로 본다 ("평균 초혼연령(아내)")
    assert dg.golden_sex("전국, 2025, 평균 초혼연령(아내)") == "여"
    # 두 성별이 다 나오면 성별로 가르지 않는다
    assert dg.golden_sex("전국, 2025, 평균 초혼연령 남편 vs 아내") is None
    assert dg.golden_sex("전국, 2021~2025, 외국인과의 혼인(한국인 남편+외국인 아내)") is None
    assert dg.golden_sex("가상시, 2025, 인구, 계") is None


def test_table_guide_numbers_and_differences_are_not_hallucination():
    guide = {
        "status": "tables",
        "answer": "추천 통계표: 통근 통학 인구(12세 이상) / 바로가기: https://kosis.kr/x?tblId=DT_1",
        "tables": {"recommendations": [{"tbl_nm": "통근 통학 인구(12세 이상)"}]},
    }
    assert dg.hallucinated_numbers(guide) == []
    diff = res(
        "남편 33.85세, 아내 31.62세로 남편이 2.23세 더 높습니다.", result={"a": 33.85, "b": 31.62}
    )
    assert dg.hallucinated_numbers(diff) == []


def test_number_position_after_link_is_kept():
    text = "링크 https://kosis.kr/statHtml/statHtml.do?orgId=101 차이 8.0%p"
    nums = dg.numbers(text)
    assert dg.find_number(dg.Expect("차이", nums[-1], True), nums, text)


def test_golden_refusal_type_reads_code():
    assert dg.golden_refusal_type("그해에 없던 지역 (region_not_in_year)") == "그해에 없던 지역"
    label = "원인·예측·평가 질문 (prediction_question)"
    assert dg.golden_refusal_type(label) == "원인·예측·평가 질문"
    assert dg.golden_refusal_type("원인·예측 질문") == "원인·예측·평가 질문"  # 옛 표기
    assert dg.golden_refusal_type("없는 연도") == "없는 연도"


def test_refusal_type_matches_by_definition_group():
    alt = {"result": {"available_years": [2012, 2025]}, "answer": "2012년부터 볼 수 있습니다."}
    want = "그해에 없던 지역 (region_not_in_year)"
    r = row(**{"기대 행동": "답변 불가", "답변 불가 유형": want})
    assert dg.grade(r, {"status": "region_not_in_year", **alt}).correct == "O"
    assert dg.grade(r, {"status": "out_of_range", **alt}).reason == "행동 (불가 유형 오류)"
    # 같은 유형의 다른 코드는 맞음
    want = "원인·예측·평가 질문 (prediction_question)"
    r = row(**{"기대 행동": "답변 불가", "답변 불가 유형": want})
    assert dg.grade(r, {"status": "causal_question", **alt}).correct == "O"
    r = row(**{"기대 행동": "답변 불가", "답변 불가 유형": "통계 질문이 아님 (chat)"})
    chat = {"status": "chat", "answer": "안녕하세요. 이런 질문을 해 주세요: …"}
    assert dg.grade(r, chat).correct == "O"


def test_allowed_answer_line_is_accepted():
    # 정답 수치의 "허용:" 줄: 다른 기준으로 계산한 값도 정답 (줄 하나를 통째로 맞아야 한다)
    r = row(
        **{
            "질문 종류": "비교",
            "정답 수치": "A 91.5, B 15.0. 차이 76.5, 6.10배\n"
            "허용: A 99.86, B 15.92. 차이 83.94, 6.27배",
        }
    )
    main = dg.grade(r, res("A가 B보다 76.5 높습니다 (6.1배).", result={"v": [91.5, 15.0]}))
    alt = dg.grade(r, res("A가 B보다 83.94 높습니다 (6.27배).", result={"v": [99.86, 15.92]}))
    half = dg.grade(r, res("A가 B보다 83.94 높습니다.", result={"v": [99.86, 15.92]}))
    assert main.number == "O" and alt.number == "O" and half.number == "X"
    assert "허용 정답" in alt.details and "허용 정답" not in main.details


def test_allowed_answer_line_with_its_own_year():
    # 허용 줄이 시점을 적었으면 조건의 기간도 그 시점으로 본다 (정답 2025, 허용 2026년 10월 말)
    r = row(
        **{
            "질문 종류": "인구 통계 지표",
            "정답 조건": "가상시, 2025, 노년부양비",
            "정답 수치": "노년부양비 35.8명\n"
            "허용: 주민등록인구 기준 노년부양비 37.66명 (2026년 10월 말)",
        }
    )
    g = dg.grade(r, res("2026년 10월 말 가상시의 노년부양비는 37.66입니다.", year=2026))
    assert g.number == "O" and g.condition == "O"
    old = dg.grade(r, res("2025년 가상시의 노년부양비는 35.8입니다.", year=2025))
    assert old.number == "O" and old.condition == "O"
