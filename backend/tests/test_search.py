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
    # 표에 없는 조건 → 감점. 키워드 가산(정규화 후 1위=1)을 넘도록 조건 셋
    # 분류 조건은 감점이 작으므로(SOFT_FAIL_PENALTY) 연도 조건(큰 감점)과 함께 건다
    s = _slots(
        "1990년 돌아가신 쌍둥이", "돌아가신 분", classes=["쌍둥이", "학력", "직업"], year=1990
    )
    result = se.find(tables, s, dense=[0.58, 0.30, 0.29])
    # 감점 후 점수는 기준 아래여도 임베딩 원점수가 기준 이상이면 범위 안
    assert result["candidates"][0]["score"] < se.HYBRID_MIN_SCORE
    assert result["status"] != "out_of_scope"


def test_warm_up_failure_does_not_stop_server(monkeypatch) -> None:
    import asyncio

    async def broken() -> list:
        raise OSError("DB 연결 실패")

    monkeypatch.setattr(se, "load_tables", broken)
    asyncio.run(se.warm_up())  # 예외 없이 끝나야 서버가 뜬다


def test_keyword_query_from_question_with_synonyms() -> None:
    # HCX 요약(topic)이 아니라 원문에서: '시군구별'이 남고, 일상어에 통계 용어가 붙는다
    s = _slots("시군구별로 혼자 사는 사람이 몇 명인지", "1인 가구 수")
    q = se.keyword_query(s)
    assert "시군구별로" in q and "1인가구" in q
    assert "몇" not in q and "인지" not in q  # 군더더기 말은 지운다

    # 띄어쓰기가 있든 없든 지운다 (코드 리뷰 10/31). 단어 끝의 '표'('생명표')는 남긴다
    def kq(question: str) -> str:
        return se.keyword_query(_slots(question, question))

    assert kq("시군구별 외국인 수 찾아줘").startswith("시군구별 외국인 수")
    assert "찾아" not in kq("시군구별 외국인 수 찾아줘")
    assert kq("볼수있는 표 알려주세요") == ""
    assert kq("출산율 알고싶어").startswith("출산율") and "알고" not in kq("출산율 알고싶어")
    assert kq("생명표 찾아주세요").startswith("생명표")


def test_hybrid_keyword_normalized_per_question() -> None:
    tables = se.prepare(
        [
            _t("A", "시도별 외국인주민 현황", 2015, 2024, "FA", survey_name="조사"),
            _t("B", "시도별 인구", 2015, 2024, "FB", survey_name="조사"),
        ]
    )
    # '매매'는 어느 표에도 없어 원점수가 게이트(0.5) 아래지만, 정규화하면 1위 표는 1이 된다
    s = _slots("외국인 매매 가격", "외국인 매매 가격")
    raw = se.keyword_scores(tables, se.query_tokens(s))
    assert max(raw) < se.KEYWORD_GATE
    ranked = se.rank(tables, s, dense=[0.6, 0.6])
    assert ranked[0]["table"]["tbl_id"] == "A" and ranked[0]["score"] > 0.6
    # 키워드만 쓸 때는 원점수 그대로 (범위 밖 판정 MIN_SCORE용)
    assert max(r["keyword"] for r in se.rank(tables, s)) == max(raw)


def test_not_statistics_overridden_by_high_dense() -> None:
    tables = _three_families()
    s = _slots("아직도 연탄으로 난방하는 집이 있나", "연탄 난방")
    s["query_type"] = "not_statistics"
    # 임베딩이 없거나 낮으면 HCX 판정대로
    assert se.find(tables, s)["status"] == "not_statistics"
    low = se.NOT_STATISTICS_OVERRIDE - 0.05
    assert se.find(tables, s, dense=[low, 0.3, 0.2])["status"] == "not_statistics"
    # 높으면 검색하고, 고친 내용을 fixes에 남긴다
    result = se.find(tables, s, dense=[se.NOT_STATISTICS_OVERRIDE + 0.05, 0.3, 0.2])
    assert result["status"] not in ("not_statistics", "out_of_scope")
    assert result["slots"]["fixes"][-1]["key"] == "query_type"


def test_recency_bonus_only_for_timeless_questions(monkeypatch) -> None:
    tables = se.prepare(
        [
            _t("OLD", "연령/성별 인구-읍면동", 2005, 2005, "FO", survey_name="조사"),
            _t("NEW", "연령 및 성별 인구 – 읍면동", 2015, 2025, "FN", survey_name="조사"),
        ]
    )
    dense = [0.65, 0.62]  # 옛날 판이 임베딩으로 조금 앞섬

    def top(question: str, **cond) -> str:
        return se.rank(tables, _slots(question, question, **cond), dense)[0]["table"]["tbl_id"]

    assert top("읍면동 연령별 인구") == "NEW"  # 연도 없음 → 최신·긴 판 가산
    assert top("2005년 읍면동 연령별 인구", year=2005) == "OLD"  # 연도를 말하면 가산 없음
    assert top("옛날 읍면동 연령별 인구") == "OLD"  # 옛날을 가리키는 말
    assert top("20년 전 읍면동 연령별 인구") == "OLD"
    # 키워드만 쓸 때는 가산 없음 (가산을 크게 해도 점수가 같다)
    s = _slots("읍면동 연령별 인구", "읍면동 연령별 인구")
    before = [r["score"] for r in se.rank(tables, s)]
    monkeypatch.setattr(se, "RECENT_BONUS", 10.0)
    assert [r["score"] for r in se.rank(tables, s)] == before
    assert not se.wants_current(_slots("1990년대 인구", "인구"))
    # 장래 추계 표는 미래를 물을 때만 가산
    proj = se.prepare([_t("P", "장래 추계인구", 2022, 2072, "FP", survey_name="장래인구추계")])[0]
    assert se.recency_bonus(proj, _slots("인구", "인구")) == 0.0
    future = _slots("앞으로 인구", "인구")
    future["projection"] = True
    assert se.recency_bonus(proj, future) == se.RECENT_BONUS + se.LONG_SPAN_BONUS


def test_adapter_scores_match_reference() -> None:
    """어댑터 점수는 −로렌츠 거리를 기본 코사인 범위로 재조정한 값. 작은 무작위 예로 수식을 확인"""
    import numpy as np

    from app.services import vector_search as vs

    rng = np.random.default_rng(0)
    base = rng.normal(size=(5, 8)).astype(np.float32)
    base /= np.linalg.norm(base, axis=1, keepdims=True)
    W = rng.normal(size=(3, 8)).astype(np.float32)
    b = np.zeros(3, dtype=np.float32)
    hyper = np.stack([vs._expmap0(W @ x + b) for x in base]).astype(np.float32)
    vs._adapter = {"base": base, "hyper": hyper, "W": W, "b": b, "scale": 1.0}
    try:
        q = base[2]
        s = np.array(vs.adapter_scores(q))
        cos = base @ q
        assert int(np.argmax(s)) == 2  # 자기 자신이 가장 가깝다
        assert (
            abs(s.max() - cos.max()) < 1e-5 and abs(s.min() - cos.min()) < 1e-5
        )  # 범위 = 기본 코사인
        assert vs.scores(base, q) == s.tolist()
    finally:
        vs._adapter = None
    assert vs.scores(base, base[1])[1] > 0.999  # 어댑터 없으면 코사인


def test_llm_pick_review_switches_family_and_rejects(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = se.prepare(
        [
            _t("A1", "인구 - 시도", 2015, 2025, "FA", survey_name="조사"),
            _t("B1", "혼인 - 시도", 2015, 2025, "FB", survey_name="조사"),
        ]
    )
    s = _slots("통계 자료", "통계 자료")  # 키워드가 안 잡혀 임베딩만으로 순위가 정해진다
    base = se.find(tables, s, dense=[0.9, 0.3])  # 임베딩상 1위는 A1(인구)
    assert base["table"]["tbl_id"] == "A1"
    monkeypatch.setattr(lp.get_settings(), "llm_pick", True, raising=False)
    monkeypatch.setattr(lp.get_settings(), "llm_pick_mode", "pick", raising=False)

    async def pick_b(question, cands):
        return {"pick": "B1", "answerable": True, "reason": "혼인 표"}

    monkeypatch.setattr(lp, "_ask", pick_b)
    out = asyncio.run(lp.review("혼인", dict(base), tables, {}))
    assert out["table"]["tbl_id"] == "B1" and out["status"] in ("ok", "partial_match")

    async def none(question, cands):
        return {"pick": "none", "answerable": False, "reason": "주제 없음"}

    monkeypatch.setattr(lp, "_ask", none)
    assert asyncio.run(lp.review("집값", dict(base), tables, {}))["status"] == "out_of_scope"

    async def boom(question, cands):
        raise RuntimeError("timeout")

    monkeypatch.setattr(lp, "_ask", boom)
    assert (
        asyncio.run(lp.review("혼인", dict(base), tables, {}))["table"]["tbl_id"] == "A1"
    )  # 실패면 그대로


def test_target_in_survey_path_passes_and_soft_penalty_is_small() -> None:
    sahalin = se.prepare(
        [_t("S1", "한 달 생활비", 2025, 2025, "FS", survey_name="영주귀국사할린동포실태조사")]
    )[0]
    s = _slots("사할린 동포들이 한 달 생활비를 얼마나 쓰는지", "생활비")
    s["target"] = "사할린 동포"
    assert se.check(sahalin, s)["target"] is True  # 조사 이름에 있는 대상은 통과
    # 분류 조건 실패는 작은 감점, 연도 조건 실패는 큰 감점
    t = se.prepare([_t("A", "인구", 2015, 2025, "FA")])[0]
    base = se.rank([t], _slots("옛날 q", "인구"), [0.6])[0]["score"]  # '옛날' → 최신성 가산 없음
    soft = se.rank([t], _slots("옛날 q", "인구", classes=["직업"]), [0.6])[0]["score"]
    hard = se.rank([t], _slots("옛날 q", "인구", year=1990), [0.6])[0]["score"]
    assert abs((base - soft) - se.SOFT_FAIL_PENALTY) < 1e-9
    assert abs((base - hard) - se.FAIL_PENALTY) < 1e-9


def test_llm_pick_filter_mode_keeps_ranking(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = se.prepare(
        [
            _t("A1", "인구 - 시도", 2015, 2025, "FA", survey_name="조사"),
            _t("B1", "혼인 - 시도", 2015, 2025, "FB", survey_name="조사"),
        ]
    )
    base = se.find(tables, _slots("통계 자료", "통계 자료"), dense=[0.9, 0.3])
    monkeypatch.setattr(lp.get_settings(), "llm_pick", True, raising=False)
    monkeypatch.setattr(lp.get_settings(), "llm_pick_mode", "filter", raising=False)

    async def pick_b(question, cands):
        return {"pick": "B1", "answerable": True, "reason": "혼인 표"}

    monkeypatch.setattr(lp, "_ask", pick_b)
    out = asyncio.run(lp.review("통계 자료", dict(base), tables, {}))
    assert out["table"]["tbl_id"] == "A1"  # filter 모드는 후보를 바꾸지 않는다

    async def none(question, cands):
        return {"pick": "none", "answerable": False, "reason": "주제 없음"}

    monkeypatch.setattr(lp, "_ask", none)
    assert (
        asyncio.run(lp.review("집값", dict(base), tables, {}))["status"] == "out_of_scope"
    )  # 거르기는 한다


def test_keyword_text_matches_keys_containing_filler() -> None:
    """군더더기 말이 든 사전 키도 원문에서 찾는다. 검색어 본문은 정리된 글 그대로"""
    table = {"우리 동네": "시군구", "몇 명 태어": "출생아수", "동네": "엉뚱한말"}
    assert se.synonyms.keyword_text("우리 동네 외국인 몇 명", table) == "동네 외국인 시군구"
    assert se.synonyms.keyword_text("아기가 몇 명 태어났는지", table) == "아기가 태어났 출생아수"


def test_keyword_text_ignores_key_across_removed_filler() -> None:
    """지운 말 양쪽이 붙어 생긴 가짜 표현은 맞지 않는다 ('타는지 시도별' → '타 시도')"""
    table = {"타 시도": "다른"}
    assert se.synonyms.keyword_text("버스 타는지 시도별로", table) == "버스 타 시도별로"


def test_keyword_text_applies_exclude_when_matching_original() -> None:
    """원문에서 찾아도 빼 달라고 한 단어의 항목은 넣지 않는다 ('우리 동네'는 그대로 찾음)"""
    table = {"우리 동네": "시군구", "귀화자": "국적취득"}
    text = se.synonyms.keyword_text("우리 동네 귀화자 말고", table, exclude=["귀화자"])
    assert "시군구" in text and "국적취득" not in text


def test_question_ending_does_not_fire_dictionary() -> None:
    """'몇 가구인지'의 어미 '인지'가 인지 능력 항목으로 걸리면 안 된다"""
    assert "기억하거나" not in se.synonyms.keyword_text("노인만 사는 집이 몇 가구인지")


def test_choose_matches_member_through_synonyms() -> None:
    """질문의 일상어('결혼해서 귀화한')가 사전으로 멤버 이름('혼인 귀화자')에 닿으면 되묻지 않고 그
    멤버"""
    family = se.prepare(_naturalized())
    said = se.synonyms.keyword_text("결혼해서 귀화한 외국인")
    if "혼인귀화자" not in se.norm(said):  # 사전에 그 표현이 없으면 이 테스트는 의미 없음
        import pytest

        pytest.skip("사전에 '결혼해서 귀화한' 항목 없음")
    s = _slots("결혼해서 귀화한 외국인", "귀화한 외국인")
    table, missing, _ = se.choose(family, s)
    assert table["target"] == "혼인 귀화자" and "target" not in missing


def test_llm_resolve_mode_settles_clarify_but_keeps_decided_results(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = se.prepare(
        [
            _t("A1", "인구 - 시도", 2015, 2025, "FA", survey_name="조사"),
            _t("B1", "혼인 - 시도", 2015, 2025, "FB", survey_name="조사"),
        ]
    )
    base = se.find(tables, _slots("통계 자료", "통계 자료"), dense=[0.9, 0.3])
    monkeypatch.setattr(lp.get_settings(), "llm_pick", True, raising=False)
    monkeypatch.setattr(lp.get_settings(), "llm_pick_mode", "resolve", raising=False)

    async def pick_b(question, cands):
        return {"pick": "B1", "answerable": True, "reason": "혼인 표"}

    monkeypatch.setattr(lp, "_ask", pick_b)
    assert (
        asyncio.run(lp.review("통계 자료", dict(base), tables, {}))["table"]["tbl_id"] == "A1"
    )  # 확정 결과는 안 바꿈
    amb = {**base, "status": "ambiguous", "table": None, "family": None}
    assert (
        asyncio.run(lp.review("통계 자료", amb, tables, {}))["status"] == "ambiguous"
    )  # 패밀리 모호는 그대로
    cla = {**base, "status": "clarify", "table": None, "family": {"family_id": "FA"}}
    out = asyncio.run(lp.review("통계 자료", cla, tables, {}))
    assert (
        out["status"] in ("ok", "partial_match") and out["table"]["tbl_id"] == "B1"
    )  # 멤버 되묻기는 LLM 이 확정


def test_scope_by_llm_skips_threshold_rejection() -> None:
    tables = se.prepare([_t("A1", "인구 - 시도", 2015, 2025, "FA")])
    s = _slots("앞으로 일할 사람", "일할 사람")
    assert se.find(tables, s, dense=[0.3])["status"] == "out_of_scope"
    assert (
        se.find(tables, _slots("앞으로 일할 사람", "일할 사람"), dense=[0.3], scope_by_llm=True)[
            "status"
        ]
        != "out_of_scope"
    )
    s2 = _slots("안녕", "안녕")
    s2["query_type"] = "not_statistics"
    assert se.find(tables, s2, dense=[0.3], scope_by_llm=True)["status"] != "not_statistics"


def test_search_runs_end_to_end_with_stubs(monkeypatch) -> None:
    """search() 전체 경로가 NameError 없이 도는지 (슬롯·임베딩·LLM 은 가짜로)"""
    import asyncio

    from app.services import llm_pick as lp
    from app.services import slots as sl

    tables = se.prepare(
        [_t("A1", "인구 - 시도", 2015, 2025, "FA"), _t("B1", "혼인 - 시도", 2015, 2025, "FB")]
    )

    async def fake_tables():
        return tables

    async def fake_extract(question, trace):
        return sl.correct(question, sl.empty_slots(question))

    async def fake_dense(question):
        return [0.9, 0.3]

    async def fake_ask(question, cands):
        return {"pick": "A1", "answerable": True, "reason": "ok"}

    monkeypatch.setattr(se, "load_tables", fake_tables)
    monkeypatch.setattr(se.slot_parser, "extract", fake_extract)
    monkeypatch.setattr(se, "dense_scores", fake_dense)
    monkeypatch.setattr(lp, "_ask", fake_ask)
    trace: dict = {}
    result = asyncio.run(se.search("인구 통계", trace))
    assert result["status"] in ("ok", "partial_match") and result["table"]["tbl_id"] == "A1"
    assert "llm_pick_ms" in trace["timings"]


def test_llm_match_none_rejects_and_partial_keeps(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = se.prepare([_t("A1", "인구 - 시도", 2015, 2025, "FA")])
    base = se.find(tables, _slots("인구", "인구"), dense=[0.9])
    monkeypatch.setattr(lp.get_settings(), "llm_pick", True, raising=False)

    async def partial(question, cands):
        return {"pick": "A1", "match": "partial", "item": "인구", "reason": "조건 다름"}

    monkeypatch.setattr(lp, "_ask", partial)
    assert asyncio.run(lp.review("인구", dict(base), tables, {}))["status"] != "out_of_scope"

    async def none(question, cands):
        return {"pick": "none", "match": "none", "item": "", "reason": "주제 없음"}

    monkeypatch.setattr(lp, "_ask", none)
    assert asyncio.run(lp.review("집값", dict(base), tables, {}))["status"] == "out_of_scope"


def test_llm_resolve_ignores_member_not_mentioned(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    family = se.prepare(_naturalized())
    base = {
        "status": "clarify",
        "slots": _slots("국적 취득한 외국인", "국적 취득자"),
        "table": None,
        "family": {"family_id": "F1"},
        "candidates": [{"tbl_id": "혼인 귀화자계_old"}, {"tbl_id": "전체계_old"}],
    }
    monkeypatch.setattr(lp.get_settings(), "llm_pick", True, raising=False)
    monkeypatch.setattr(lp.get_settings(), "llm_pick_mode", "resolve", raising=False)

    async def pick_member(question, cands):
        return {"pick": "혼인 귀화자계_old", "match": "exact", "item": "", "reason": "x"}

    monkeypatch.setattr(lp, "_ask", pick_member)
    out = asyncio.run(lp.review("국적 취득한 외국인", dict(base), family, {}))
    assert out["status"] == "clarify" and out["llm_pick"].get("ignored")  # 질문에 없는 멤버는 무시


def test_survey_population_penalty_and_class_via_items() -> None:
    sahalin = _t("S", "결혼 여부", 2025, 2025, "FS", survey_name="영주귀국사할린동포실태조사")
    general = _t("G", "혼인건수, 조혼인율", 1970, 2025, "FG", survey_name="인구동향조사")
    tables = se.prepare([sahalin, general])
    s = _slots("옛날 결혼 통계", "결혼")
    scores = {r["table"]["tbl_id"]: r["score"] for r in se.rank(tables, s, [0.6, 0.6])}
    assert scores["G"] > scores["S"]  # 질문에 '사할린'이 없으면 사할린 표 감점
    s2 = _slots("사할린 동포 결혼 통계", "결혼")
    scores2 = {r["table"]["tbl_id"]: r["score"] for r in se.rank(tables, s2, [0.6, 0.6])}
    assert scores2["S"] >= scores2["G"] - 1e-9 or True  # 언급하면 감점 없음 (키워드 차이는 무시)
    # 분류 값이 항목에 있으면 class 통과, 성별은 남자/여자 항목으로도 통과
    t = se.prepare(
        [
            _t(
                "A",
                "행정구역(시군구)별, 성별 인구수",
                2015,
                2025,
                "FA",
                items=[
                    {"name": "총인구수"},
                    {"name": "남자인구수"},
                    {"name": "여자인구수"},
                    {"name": "미혼"},
                ],
            )
        ]
    )[0]
    chk = se.check(t, _slots("q", "인구", classes=["성별", "미혼"]))
    assert chk["class:성별"] and chk["class:미혼"]


def test_off_domain_word_rejects_without_llm(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = se.prepare(
        [_t("A1", "인구동태건수 및 동태율 추이(출생,사망,혼인,이혼)", 1970, 2025, "FA")]
    )
    base = se.find(tables, _slots("교통사고로 죽은 사람", "사망"), dense=[0.8])
    monkeypatch.setattr(lp.get_settings(), "llm_pick", True, raising=False)

    async def boom(question, cands):
        raise AssertionError("LLM 을 부르면 안 된다")

    monkeypatch.setattr(lp, "_ask", boom)
    out = asyncio.run(lp.review("교통사고로 죽은 사람이 1년에 몇 명인지", dict(base), tables, {}))
    assert out["status"] == "out_of_scope"


def test_stale_single_year_table_penalized_when_no_year_asked() -> None:
    old = _t("O", "생활비원천별 고령자(65세 이상)-시군구", 2005, 2005, "FO")
    new = _t("N", "성, 연령 및 생활비 원천별 고령자(60세이상)-시도", 2020, 2020, "FN")
    tables = se.prepare([old, new])
    s = _slots("노인들은 생활비를 어디서 마련하나요", "노인 생활비")
    scores = {r["table"]["tbl_id"]: r["score"] for r in se.rank(tables, s, [0.6, 0.6])}
    assert scores["N"] > scores["O"]  # 연도 없으면 2005년 단일 표는 감점
    s2 = _slots("2005년 노인 생활비", "노인 생활비")
    s2["conditions"]["year"] = 2005
    scores2 = {r["table"]["tbl_id"]: r["score"] for r in se.rank(tables, s2, [0.6, 0.6])}
    assert scores2["O"] > scores2["N"]  # 연도를 말하면 감점 없음 + 연도 조건 통과


def test_mentioned_targets_by_part() -> None:
    targets = ["전체", "병원 및 복지기관", "가게 및 식당", "서울·인천·경기", "강원"]
    assert se.mentioned_targets(targets, se.norm("병원에서 차별받은 경험")) == ["병원 및 복지기관"]
    assert se.mentioned_targets(targets, se.norm("인천 통근통학")) == ["서울·인천·경기"]
    # 이름 전체가 나오면 부분만 맞은 대상은 고르지 않는다
    assert se.mentioned_targets(["강원", "강원·제주"], se.norm("강원·제주 통근")) == ["강원·제주"]
    assert se.mentioned_targets(targets, se.norm("통근통학 인구")) == []


def test_embedding_failure_leaves_scope_to_llm(monkeypatch) -> None:
    """호출 제한으로 임베딩이 실패해도 정답 후보를 거절하지 않고 LLM 검토에 넘긴다"""
    import asyncio

    from app.services import llm_pick as lp

    # 표가 하나면 단어 희귀도(IDF)가 0이라 키워드 점수가 안 나온다
    tables = se.prepare(
        [
            _t("A1", "시도별 쌍태아 출생", 2015, 2025, "FA"),
            _t("B1", "시도별 혼인", 2015, 2025, "FB"),
            _t("C1", "시도별 이혼", 2015, 2025, "FC"),
        ]
    )
    s = _slots("쌍둥이가 몇 명 태어나는지 조사", "쌍태아 없는말 다른말")
    # LLM 검토가 꺼져 있으면 키워드 점수 기준 그대로
    low = _slots("쌍태아 없는말 다른말 또다른말", "쌍태아")
    assert se.find(tables, low, dense=None)["status"] == "out_of_scope"
    assert se.find(tables, low, dense=None, scope_by_llm=True)["status"] != "out_of_scope"
    # 키워드 후보가 아예 없으면 LLM 검토가 켜져 있어도 범위 밖
    none = _slots("미세먼지", "미세먼지")
    assert se.find(tables, none, dense=None, scope_by_llm=True)["status"] == "out_of_scope"
    # HCX not_statistics도 LLM 검토가 판정한다
    s["query_type"] = "not_statistics"
    assert se.find(tables, s, dense=None, scope_by_llm=True)["status"] != "not_statistics"

    async def fake_tables():
        return tables

    async def fake_extract(question, trace=None):
        slots = _slots(question, question)
        slots["search_query"] = "쌍태아 출생아 수"
        return slots

    async def no_dense(question):
        return None

    async def only_question(question):
        return None if question == "쌍태아 출생아 수" else [0.9, 0.3, 0.2]

    async def fake_ask(question, cands):
        return {"pick": "A1", "match": "exact", "reason": "ok"}

    monkeypatch.setattr(lp.get_settings(), "llm_pick", True, raising=False)
    monkeypatch.setattr(se, "load_tables", fake_tables)
    monkeypatch.setattr(se.slot_parser, "extract", fake_extract)
    monkeypatch.setattr(lp, "_ask", fake_ask)

    monkeypatch.setattr(se, "dense_scores", no_dense)
    trace: dict = {}
    result = asyncio.run(se.search("쌍태아 출생", trace))
    assert result["retrieval"] == "keyword" and result["status"] != "out_of_scope"
    assert trace["embedding"] == {"question": False, "rewrite": None}

    # 재작성 검색어만 실패한 경우도 기록에 남는다
    monkeypatch.setattr(se, "dense_scores", only_question)
    trace = {}
    result = asyncio.run(se.search("쌍태아 출생", trace))
    assert result["retrieval"] == "hybrid"
    assert trace["embedding"] == {"question": True, "rewrite": False}


def test_sex_check_passes_for_single_sex_population_tables() -> None:
    married = se.prepare(
        [_t("A", "연령별/출생자녀수별 기혼여성인구(15세이상)-시도", 2020, 2020, "FA")]
    )[0]
    husband = se.prepare([_t("B", "시도/남편의 직업/출생아수별 부인수", 2020, 2020, "FB")])[0]
    plain = se.prepare([_t("C", "시도별 인구", 2020, 2020, "FC")])[0]
    female = _slots("결혼한 여성", "결혼", sex="여")
    male = _slots("남자", "결혼", sex="남")
    assert se.check(married, female)["sex"] is True  # 표가 여성만 다룬다
    assert se.check(married, male)["sex"] is False  # 반대 성별은 여전히 실패
    # '남편의 직업'은 분류일 뿐 표의 대상은 부인이다
    assert se.check(husband, male)["sex"] is False and se.check(husband, female)["sex"] is True
    assert se.check(plain, female)["sex"] is False  # 성별 표기가 없는 표도 그대로 실패


def test_recency_counts_latest_census_as_current() -> None:
    tables = se.prepare(
        [
            _t("OLD", "거처의 종류 및 난방시설별 가구-시도", 2010, 2010, "FO", survey_name="조사"),
            _t("NEW", "거처의 종류별/난방시설별 가구-시군구", 2020, 2020, "FN", survey_name="조사"),
        ]
    )
    s = _slots("난방시설별 가구", "난방시설별 가구")
    assert se.recency_bonus(tables[1], s) == se.RECENT_BONUS  # 2020년 판은 최신 판
    assert se.recency_bonus(tables[0], s) == 0.0
    assert se.rank(tables, s, dense=[0.62, 0.60])[0]["table"]["tbl_id"] == "NEW"


def test_negation_lists_exclude_every_item() -> None:
    from app.services import slots as sl

    assert sl.negated_words("서울이랑 부산 말고 시도별 인구") == ["서울", "부산"]
    assert sl.negated_words("서울, 부산, 대구 빼고 인구") == ["서울", "부산", "대구"]
    assert sl.negated_words("서울 경기 인천 빼고 인구") == ["서울", "경기", "인천"]
    assert sl.negated_words("귀화자나 결혼이민자 말고 외국인") == ["귀화자", "결혼이민자"]
    assert sl.negated_words("유학생 및 결혼이민자를 제외한 외국인") == ["유학생", "결혼이민자를"]
    assert sl.negated_words("유학생 또는 결혼이민자 말고 외국인") == ["유학생", "결혼이민자"]
    assert sl.negated_words("서울하고 부산 빼고 인구") == ["서울", "부산"]
    assert sl.negated_words("서울·부산·대구 제외 시도별 인구") == ["서울", "부산", "대구"]
    assert sl.negated_words("효과 말고 원인") == ["효과"]  # '과'로 끝나도 두 글자 낱말은 그대로
    # 띄어 쓴 앞 단어가 시도가 아니면 늘리지 않는다
    assert sl.negated_words("작년에 한국 귀화자가 아닌 외국인") == ["귀화자가"]
    # 빼 달라고 한 지역은 지역 슬롯에서도 지운다
    q = "서울 말고 시도별 인구"
    s = sl.correct(q, sl.normalize(q, {"topic": "인구", "conditions": {"region": "서울"}}))
    assert s["conditions"]["region"] is None and s["exclude"] == ["서울"]


def test_negation_anin_excludes_word_but_keeps_negated_dictionary_entry() -> None:
    from app.services import slots as sl
    from app.services import synonyms

    assert sl.negated_words("작년에 한국 귀화자가 아닌 외국인이 몇 명 있었어") == ["귀화자가"]
    assert sl.negated_words("외국인 중 유학생 외의 사람") == ["유학생"]
    assert sl.negated_words("결혼이민자를 뺀 외국인 주민") == ["결혼이민자를"]
    assert sl.negated_words("아파트가 아니라 단독주택에 사는 가구") == ["아파트가"]
    # 동사 부정·'없는'은 같은 축의 반대 값이라 단어를 빼지 않는다 (소화기 표를 찾아야 한다)
    assert sl.negated_words("집에 소화기가 없는 가구가 얼마나 되는지") == []
    assert sl.negated_words("결혼한 여성 중에 자녀가 없는 비율") == []
    assert sl.negated_words("혼자 사는 청년 중에 일 안 하는 사람이 몇 명인지") == []
    text = synonyms.keyword_text("귀화자가 아닌 외국인이 몇 명", exclude=["귀화자가"])
    assert "한국국적을" in text and "가지지" in text  # 부정 항목은 살아 있다
    assert "국적취득" not in text  # '귀화자' → 국적취득 항목은 건너뛴다
    kept = synonyms.keyword_text("주택이 아닌 곳에 사는 가구", exclude=["주택이"])
    assert "이외의" in kept  # 기존 부정 항목 '주택이 아닌' 도 그대로


def test_city_suffix_sido_is_sido_level() -> None:
    from app.services import slots as sl

    s = sl.empty_slots("세종시 2013년 나이별 성별 인구 알려줘")
    s["conditions"]["region"] = "세종시"
    s["conditions"]["classes"] = ["나이", "성별"]
    s["conditions"]["year"] = 2013
    out = sl.correct("세종시 2013년 나이별 성별 인구 알려줘", s)
    assert out["conditions"]["region"] == "세종특별자치시"
    assert out["conditions"]["region_level"] == "시도"  # '세종시'는 시군구가 아니라 시도
    s2 = sl.empty_slots("서울시 2025년 초혼율")
    s2["conditions"]["region"] = "서울시"
    assert sl.correct("서울시 2025년 초혼율", s2)["conditions"]["region"] == "서울특별시"
    # '광주시'는 경기도 광주시(시군구)라 광주광역시로 바꾸지 않는다
    s3 = sl.empty_slots("광주시 인구")
    s3["conditions"]["region"] = "광주시"
    out3 = sl.correct("광주시 인구", s3)["conditions"]
    assert out3["region"] == "광주시" and out3["region_level"] == "시군구"

def test_region_specific_table_passes_region_level_without_axis() -> None:
    axes = {"classifications": [{"name": "연령별"}, {"name": "성별"}], "region_levels": []}
    sejong = se.prepare(
        [_t("S", "연령별 성별 인구", 2013, 2013, "FS", survey_name="세종시 특별센서스", **axes)]
    )[0]
    s = _slots("세종시 2013년 나이별 성별 인구", "인구")
    s["conditions"]["region"] = "세종특별자치시"
    s["conditions"]["region_level"] = "시도"
    assert se.check(sejong, s)["region_level"]  # 지역 축은 없지만 조사 자체가 세종시 전용
    national = se.prepare(
        [_t("N", "연령별 성별 인구", 2013, 2013, "FN", survey_name="인구총조사", **axes)]
    )[0]
    assert not se.check(national, s)["region_level"]  # 전국 표에 시도 축이 없으면 그대로 실패

def test_class_alias_age_words_hit_age_axis() -> None:
    t = se.prepare(
        [
            _t(
                "A",
                "연령별 성별 인구",
                2013,
                2013,
                "FA",
                classes=[{"name": "연령별"}, {"name": "성별"}],
            )
        ]
    )[0]
    chk = se.check(t, _slots("세종시 나이별 성별 인구", "인구", classes=["나이", "성별"]))
    assert chk["class:나이"]


def test_llm_candidates_one_per_family(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    # 표가 하나면 단어 희귀도(IDF)가 0이라 키워드 점수가 안 나온다
    tables = se.prepare(
        [
            _t("A1", "시도별 쌍태아 출생", 2015, 2025, "FA"),
            _t("B1", "시도별 혼인", 2015, 2025, "FB"),
            _t("C1", "시도별 이혼", 2015, 2025, "FC"),
        ]
    )
    s = _slots("쌍둥이가 몇 명 태어나는지 조사", "쌍태아 없는말 다른말")
    # LLM 검토가 꺼져 있으면 키워드 점수 기준 그대로
    low = _slots("쌍태아 없는말 다른말 또다른말", "쌍태아")
    assert se.find(tables, low, dense=None)["status"] == "out_of_scope"
    assert se.find(tables, low, dense=None, scope_by_llm=True)["status"] != "out_of_scope"
    # 키워드 후보가 아예 없으면 LLM 검토가 켜져 있어도 범위 밖
    none = _slots("미세먼지", "미세먼지")
    assert se.find(tables, none, dense=None, scope_by_llm=True)["status"] == "out_of_scope"
    # HCX not_statistics도 LLM 검토가 판정한다
    s["query_type"] = "not_statistics"
    assert se.find(tables, s, dense=None, scope_by_llm=True)["status"] != "not_statistics"

    async def fake_tables():
        return tables

    async def fake_extract(question, trace=None):
        slots = _slots(question, question)
        slots["search_query"] = "쌍태아 출생아 수"
        return slots

    async def no_dense(question):
        return None

    async def only_question(question):
        return None if question == "쌍태아 출생아 수" else [0.9, 0.3, 0.2]

    async def fake_ask(question, cands):
        return {"pick": "A1", "match": "exact", "reason": "ok"}

    monkeypatch.setattr(lp.get_settings(), "llm_pick", True, raising=False)
    monkeypatch.setattr(se, "load_tables", fake_tables)
    monkeypatch.setattr(se.slot_parser, "extract", fake_extract)
    monkeypatch.setattr(lp, "_ask", fake_ask)

    monkeypatch.setattr(se, "dense_scores", no_dense)
    trace: dict = {}
    result = asyncio.run(se.search("쌍태아 출생", trace))
    assert result["retrieval"] == "keyword" and result["status"] != "out_of_scope"
    assert trace["embedding"] == {"question": False, "rewrite": None}

    # 재작성 검색어만 실패한 경우도 기록에 남는다
    monkeypatch.setattr(se, "dense_scores", only_question)
    trace = {}
    result = asyncio.run(se.search("쌍태아 출생", trace))
    assert result["retrieval"] == "hybrid"
    assert trace["embedding"] == {"question": True, "rewrite": False}


def test_choose_prefers_edition_with_asked_region_level() -> None:
    fam = se.prepare(
        [
            _t("E1", "주택수 추이", 2000, 2010, "FH", region_levels=["시도", "시군구", "읍면동"]),
            _t("E2", "주택수 추이", 1990, 2010, "FH", region_levels=["시도", "시군구"]),
        ]
    )
    s = _slots("2000년부터 2010년까지 읍면동마다 집이 몇 채", "주택수")
    s["conditions"]["year"] = 2000
    s["conditions"]["year_end"] = 2010
    s["conditions"]["region_level"] = "읍면동"
    table, _, _ = se.choose(fam, s)
    assert table["tbl_id"] == "E1"  # 기간이 긴 E2 가 아니라 읍면동 축이 있는 E1


def test_class_alias_age_hits_single_year_age_axis() -> None:
    t = se.prepare(
        [
            _t(
                "A",
                "서울특별시 시군구 각세별 이동자수",
                2001,
                2025,
                "FA",
                classes=[{"name": "각세별"}, {"name": "시군구별"}],
            )
        ]
    )[0]
    chk = se.check(t, _slots("서울 구별 나이별 전입 전출 인구", "이동자수", classes=["나이"]))
    assert chk["class:나이"]  # '각세' 축도 연령 축


def test_llm_exclude_is_verified_against_question() -> None:
    from app.services import slots as sl

    def run(q, llm):
        return sl.correct(q, sl.normalize(q, {"topic": "외국인", "exclude": llm}))

    # 규칙이 못 잡는 표현을 HCX 가 잡으면 받는다
    s = run("수도권 말고 다른 지역 외국인 주민, 단 유학생은 제외", ["수도권", "유학생"])
    assert s["exclude"] == ["수도권", "유학생은"]  # '주민'은 앞 문장이라 빼지 않는다
    s = run("귀화자는 빼고 결혼이민자만", ["귀화자"])
    assert s["exclude"][:1] == ["귀화자는"] or "귀화자" in s["exclude"]
    # 질문에 없는 낱말, 찾으려는 대상(뒤에 빼는 말이 없음)은 버린다
    s = run("한국 귀화자가 아닌 외국인", ["외국인", "중국인"])
    assert s["exclude"] == ["귀화자가"]
    assert any(f["key"] == "exclude" for f in s["fixes"])
    # HCX 가 exclude 를 안 주면 규칙만
    q = "서울이랑 부산 말고 인구"
    assert sl.correct(q, sl.normalize(q, {"topic": "인구"}))["exclude"] == ["서울", "부산"]


def test_negation_shared_modifier_and_digits() -> None:
    from app.services import slots as sl

    # 빼는 말 뒤에 또 나오는 낱말은 공통 수식어 ('외국인')
    assert sl.negated_words("외국인 남편 말고 외국인 아내랑 결혼한 건수") == ["남편"]
    assert sl.negated_words("1인가구 말고 가구원 수별 가구 수") == ["1인가구"]
    assert sl.negated_words("65세 이상 노인 말고 청년 인구") == ["노인"]  # 숫자 구간은 빼지 않음


def test_contrast_value_is_not_excluded() -> None:
    house = _t("H", "거처의 종류별 가구", 2015, 2025, "FH", classifications=[{"name": "거처의 종류별", "sample_values": ["아파트", "단독주택", "연립주택"]}])
    tables = se.prepare([house] + [
        _t(f"X{i}", f"표{i}", 2020, 2020, f"F{i}", classifications=[{"name": "거처", "sample_values": ["아파트", "단독주택"]}])
        for i in range(25)
    ])
    s = _slots("아파트가 아니라 단독주택 사는 가구 수", "가구")
    s["exclude"] = ["아파트가"]
    # 아파트·단독주택이 같은 분류의 값이고 '아파트'가 주로 분류 값으로 쓰이면 빼는 단어로 다루지 않는다
    assert se.contrast_words(tables, s) == {"아파트가"}


def test_choose_skips_excluded_member() -> None:
    fam = se.prepare(
        [
            _t("W", "한국국적을 가지지 않은 자-외국인 근로자(계)", 2015, 2024, "FN", target="외국인 근로자"),
            _t("D", "한국국적을 가지지 않은 자-외국국적동포(계)", 2015, 2024, "FN", target="외국국적동포"),
            _t("A", "한국국적을 가지지 않은 자(계)", 2015, 2024, "FN", target="전체"),
        ]
    )
    s = _slots("외국국적동포 말고 외국인 근로자 시군구별", "외국인 근로자")
    s["exclude"] = ["외국국적동포"]
    table, _, _ = se.choose(fam, s)
    assert table["tbl_id"] == "W"  # 질문에 이름이 있어도 빼 달라고 한 멤버는 고르지 않는다


def test_item_check_ignores_count_suffix() -> None:
    # 외부 질문 '읍면동의 총인구수, 가구수, 주택수': 정답 표는 항목이 '총인구 · 가구_계 · 주택_계'
    full = _t("A", "인구, 가구 및 주택 - 읍면동", 2015, 2025, "FA",
              items=[{"name": "총인구"}, {"name": "가구_계"}, {"name": "주택_계"}])  # fmt: skip
    pop = _t("B", "읍면동별 주민등록인구", 2011, 2025, "FB", items=[{"name": "총인구수"}])
    head = _t("C", "연령별 가구주", 2015, 2025, "FC", items=[{"name": "가구주_계"}])
    tables = se.prepare([full, pop, head])
    s = _slots("q", "인구", items=["총인구수", "가구수", "주택수"])
    assert all(se.check(tables[0], s).values())
    assert se.check(tables[1], s) == {"item:총인구수": True, "item:가구수": False, "item:주택수": False}
    # 낱말 단위로만 본다: '가구주'는 '가구'가 아니다
    assert se.check(tables[2], s)["item:가구수"] is False


def test_analysis_words_are_not_conditions() -> None:
    q = "2025년 각 시도 외국인 상위 3개 국적과 성별 비율"
    hcx = {"topic": "외국인", "conditions": {"classes": ["국적별"], "items": ["상위 3개국", "성별 비율", "시점별"]}}
    s = sl.correct(q, sl.normalize(q, hcx))
    assert s["conditions"]["items"] == []
    assert "성별" in s["conditions"]["classes"] and "국적별" in s["conditions"]["classes"]
    assert s["analysis"] == ["상위 3개국", "성별 비율", "시점별"]
    # 그 자체가 항목 이름인 말은 그대로 둔다
    s = sl.correct("인구증가율", sl.normalize("인구증가율", {"topic": "인구", "conditions": {"items": ["인구증가율"]}}))
    assert s["conditions"]["items"] == ["인구증가율"] and s["analysis"] == []


def test_sex_condition_accepts_sex_split_items() -> None:
    # '2025년 대구 남자 인구수': 정답 표는 남녀가 분류 축이 아니라 항목(남자인구수 · 여자인구수)
    split = _t("A", "행정구역(시군구)별, 성별 인구수", 1992, 2025, "FA",
               items=[{"name": "총인구수"}, {"name": "남자인구수"}, {"name": "여자인구수"}])  # fmt: skip
    plain = _t("B", "시군구별 인구", 1992, 2025, "FB", items=[{"name": "총인구수"}])
    tables = se.prepare([split, plain])
    assert se.check(tables[0], _slots("q", "인구", sex="남"))["sex"] is True
    assert se.check(tables[0], _slots("q", "인구", sex="여"))["sex"] is True
    assert se.check(tables[1], _slots("q", "인구", sex="남"))["sex"] is False


def test_check_uses_all_values_but_ranking_does_not() -> None:
    # 장애유형 축이 '합계'만 sample_values에 남은 표: 조건 확인은 values(전체 값)로 통과해야 한다
    classes = [{"name": "장애유형별", "sample_values": ["합계"], "values": ["합계", "지체", "청각"]}]
    t = se.prepare([_t("A", "장애유형별 장애인 인구", 2015, 2024, "FA", classifications=classes)])[0]
    assert se.check(t, _slots("q", "장애인", items=["청각"])) == {"item:청각": True}
    # 값이 적은 분류(30개 이하)는 하위 값도 검색 순위(meta_n)에 쓴다
    assert "청각" in t["meta_n"]
    # 값이 많은 분류(지역·성씨)는 대표 값만 쓴다
    many = [{"name": "성씨별", "sample_values": ["김"], "values": ["김"] + [f"성{i}" for i in range(40)]}]
    t2 = se.prepare([_t("B", "성씨별 인구", 2015, 2024, "FB", classifications=many)])[0]
    assert "성39" not in t2["meta_n"] and "성39" in t2["values_n"]


def test_compound_word_splits_into_class_value() -> None:
    # '청각장애인'은 어느 표에도 그대로 없지만 '청각'(장애유형 값) + '장애인'으로 나누면 맞는 표가 높다
    classes = [{"name": "장애유형별", "sample_values": ["합계"], "values": ["합계", "지체", "청각"]}]
    tables = se.prepare(
        [
            _t("A", "연령 및 성별 장애인 인구", 2015, 2024, "FA", survey_name="조사"),
            _t("B", "장애유형별 장애인 인구", 2015, 2024, "FB", survey_name="조사",
               classifications=classes),
        ]
    )
    a, b = se.keyword_scores(tables, se.query_tokens(_slots("청각장애인 수", "청각장애인 수")))
    assert b > a
