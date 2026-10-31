from app.services import search as se
from app.services import slots as sl


def _t(tbl_id: str, name: str, start: int, end: int, family: str, **extra) -> dict:
    row = {
        "tbl_id": tbl_id,
        "table_name": name,
        "survey_name": "지방자치단체외국인주민현황",
        "organization": "행정안전부",
        "period": f"년 ({start} ~ {end})",
        "path_texts": [f"주제별통계 > 인구 > {name}"],
        "classifications": [{"name": "행정구역별", "sample_values": ["서울특별시"]}],
        "items": [{"name": "계"}],
        "start_year": start,
        "end_year": end,
        "region_levels": ["시도", "시군구"],
        "prev_region_levels": [],
        "sex_coverage": "none",
        "has_total": None,
        "url": f"https://kosis.kr/{tbl_id}",
        "family_id": family,
        "family_name": name,
        "target": None,
        "sex": None,
        "edition_order": 1,
        "edition": f"{start}~{end}",
    }
    row.update(extra)
    return row


def _naturalized() -> list[dict]:
    """한국국적을 취득한 자 패밀리 축소판: 대상 2 × 성별 2 × 판"""
    rows = []
    for target, suffix in (("전체", ""), ("혼인 귀화자", "-혼인 귀화자")):
        for sex in ("계", "여"):
            name = f"한국국적을 취득한 자{suffix}({sex})"
            rows.append(
                _t(f"{target}{sex}_old", f"{name}(2007~2015)", 2007, 2015, "F1",
                   family_name="한국국적을 취득한 자", target=target, sex=sex,
                   edition_order=1, sex_coverage="total_only" if sex == "계" else "female_only")
            )  # fmt: skip
            rows.append(
                _t(f"{target}{sex}_new", name, 2015, 2015, "F1",
                   family_name="한국국적을 취득한 자", target=target, sex=sex,
                   edition_order=2, sex_coverage="total_only" if sex == "계" else "female_only")
            )  # fmt: skip
    return rows


def _slots(question: str, topic: str, **cond) -> dict:
    s = sl.empty_slots(question)
    s["topic"] = topic
    s["conditions"].update(cond)
    return s


def test_keyword_scores_weight_rare_words() -> None:
    tables = se.prepare(
        [
            _t("A", "시도별 외국인주민 현황", 2015, 2024, "FA", survey_name="조사"),
            _t("B", "시도별 인구", 2015, 2024, "FB", survey_name="조사"),
            _t("C", "시군구별 인구", 2015, 2024, "FC", survey_name="조사"),
        ]
    )
    tokens = se.query_tokens(_slots("외국인 인구", "외국인 인구"))
    a, b, _ = se.keyword_scores(tables, tokens)
    # 흔한 '인구'만 맞는 표보다 드문 '외국인'이 맞는 표가 높다
    assert a > b


def test_check_conditions() -> None:
    t = se.prepare([_t("A", "성별 인구", 2010, 2020, "FA", sex_coverage="both", has_total=False)])[
        0
    ]
    s = _slots("q", "인구", year=2015, region_level="읍면동", sex="전체", classes=["성별"])
    assert se.check(t, s) == {
        "year": True,
        "region_level": False,
        "sex": False,
        "class:성별": True,
    }
    # 질문에 없는 조건은 판정하지 않는다
    assert se.check(t, _slots("q", "인구")) == {}


def test_choose_member_and_edition() -> None:
    family = se.prepare(_naturalized())
    # 대상·성별을 말하면 그 멤버, 연도가 두 판에 다 있으면 기간이 긴 판
    s = _slots("2015년 혼인 귀화자 여자", "귀화자", year=2015, sex="여")
    table, missing, notes = se.choose(family, s)
    assert (table["tbl_id"], missing, notes) == ("혼인 귀화자여_old", [], [])
    # 대상·성별을 말하지 않으면 전체·계 멤버의 기본 판, 빠진 축을 알려준다
    table, missing, _ = se.choose(family, _slots("국적 취득자", "국적 취득자"))
    assert (table["tbl_id"], missing) == ("전체계_old", ["target", "sex"])
    # 해당 연도 판이 없으면 가장 가까운 판 + 메모 (거리가 같으면 기본 판 = 기간이 긴 판)
    table, _, notes = se.choose(family, _slots("2020년 국적 취득자", "국적 취득자", year=2020))
    assert table["tbl_id"] == "전체계_old" and notes


def test_find_statuses() -> None:
    tables = se.prepare(_naturalized())
    weather = _slots("오늘 날씨", "날씨")
    weather["query_type"] = "not_statistics"
    assert se.find(tables, weather)["status"] == "not_statistics"
    assert se.find(tables, _slots("주택 가격", "주택 가격"))["status"] == "out_of_scope"

    result = se.find(tables, _slots("2010년 혼인 귀화자 여자", "혼인 귀화자", year=2010, sex="여"))
    assert result["status"] == "ok"
    assert result["table"]["tbl_id"] == "혼인 귀화자여_old"
    assert result["family"]["axes"]["target"] == ["전체", "혼인 귀화자"]


def _three_families() -> list[dict]:
    return se.prepare(
        [
            _t("A", "시군구/월별 사망자수", 2015, 2024, "FA"),
            _t("B", "시도별 인구", 2015, 2024, "FB"),
            _t("C", "시도별 외국인주민 현황", 2015, 2024, "FC"),
        ]
    )


def test_hybrid_finds_table_without_keyword() -> None:
    tables = _three_families()
    s = _slots("돌아가신 분 수", "돌아가신 분 수")
    # 키워드만으로는 '돌아가신'이 표명에 없어 찾지 못한다
    assert se.find(tables, s)["status"] == "out_of_scope"
    result = se.find(tables, s, dense=[0.62, 0.40, 0.35])
    assert result["retrieval"] == "hybrid"
    assert result["status"] == "ok" and result["table"]["tbl_id"] == "A"
    assert result["candidates"][0]["dense"] == 0.62


def test_hybrid_adds_keyword_only_above_gate() -> None:
    assert se.relevance(0.9, 0.0, 0.5) == 0.5 + se.KEYWORD_WEIGHT * 0.9
    assert se.relevance(se.KEYWORD_GATE - 0.01, 0.5, 0.5) == 0.5  # 표명 유사도도 함께 빠진다
    # 임베딩이 없으면 예전처럼 키워드 + 표명 유사도
    assert se.relevance(0.4, 0.5, None) == 0.4 + se.NAME_WEIGHT * 0.5


def test_hybrid_candidates_and_thresholds() -> None:
    tables = _three_families()
    s = _slots("오늘 날씨", "날씨")
    # 키워드 0점이어도 임베딩 상위는 후보에 들고, 낮은 점수는 out_of_scope
    result = se.find(tables, s, dense=[0.30, 0.31, 0.29])
    assert len(result["candidates"]) == 3 and result["status"] == "out_of_scope"
    # 1·2순위 패밀리가 거의 동점이면 ambiguous
    result = se.find(tables, s, dense=[0.600, 0.598, 0.30])
    assert result["status"] == "ambiguous"
    assert [o["family_id"] for o in result["options"]] == ["FA", "FB"]


def test_rule_conditions_projection_spouse_exclude() -> None:
    tables = se.prepare(
        [
            _t("P", "시나리오별 추계인구", 2022, 2072, "FP", survey_name="장래인구추계"),
            _t("W", "시도/시군구/외국인 아내의 국적별 혼인", 2015, 2024, "FW"),
            _t("H", "시도/시군구/외국인 남편의 국적별 혼인", 2015, 2024, "FH"),
            _t("N", "한국국적을 취득한 자-혼인 귀화자(계)", 2015, 2024, "FN"),
        ]
    )
    p, w, h, n = tables

    def rule_slots(q: str) -> dict:
        return sl.correct(q, sl.empty_slots(q))

    s = rule_slots("2050년 인구")
    assert se.check(p, s)["projection"] and not se.check(w, s)["projection"]

    s = rule_slots("외국인 여자와 결혼한 한국 남자")
    assert se.check(w, s)["spouse"] and not se.check(h, s)["spouse"]

    s = rule_slots("귀화한 사람 말고")
    assert not se.check(n, s)["exclude"] and se.check(w, s)["exclude"]
    # 빼 달라고 한 단어는 키워드 검색어에서도 뺀다
    assert all("귀화" not in t["variants"] for t in se.query_tokens(s))


def test_unasked_subset_modifier_yields_to_plain_sibling() -> None:
    tables = se.prepare(
        [
            _t("B", "연령/교육정도별 혼인", 2015, 2024, "FB"),
            _t("F", "연령/교육정도별 혼인(초혼)", 2015, 2024, "FF"),
        ]
    )
    # 임베딩 점수는 수식어 표가 조금 높아도, 초혼을 말하지 않았으면 수식어 없는 표가 1순위
    s = _slots("연령별 결혼 건수", "결혼 건수")
    assert se.rank(tables, s, dense=[0.600, 0.605])[0]["table"]["tbl_id"] == "B"
    # '처음 결혼'은 초혼을 말한 것
    s = _slots("처음 결혼한 사람 연령별", "처음 결혼")
    assert se.rank(tables, s, dense=[0.600, 0.605])[0]["table"]["tbl_id"] == "F"


def test_hybrid_scope_uses_raw_dense_not_penalized_score() -> None:
    tables = _three_families()
    s = _slots("돌아가신 쌍둥이", "돌아가신 분", classes=["쌍둥이"])  # 표에 없는 조건 → 감점
    result = se.find(tables, s, dense=[0.58, 0.30, 0.29])
    # 감점 후 점수는 기준 아래여도 임베딩 원점수가 기준 이상이면 범위 안
    assert result["candidates"][0]["score"] < se.HYBRID_MIN_SCORE
    assert result["status"] != "out_of_scope"
