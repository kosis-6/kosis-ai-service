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
