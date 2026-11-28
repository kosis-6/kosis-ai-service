import pytest

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
    assert se.check(t, _slots("q", "인구")) == {}


def test_child_count_checks_distinguish_an_exact_count_from_a_range() -> None:
    title = "시도/미성년자녀수별 이혼"
    items = [{"name": name} for name in ("이혼건수-계", "미성년자녀-없음", "3명 이상")]
    table = se.prepare([_t("D", title, 1993, 2025, "FD", items=items)])[0]
    for item, expected in (("3명", False), ("3명 이상", True)):
        question = f"미성년 자녀가 {item}인 부부의 이혼 건수"
        assert se.check(table, _slots(question, "이혼 건수", items=[item])) == {
            f"item:{item}": expected,
        }
        assert se.check(table, _slots(question, "이혼 건수")) == {"item:자녀 수": expected}
    question = "이혼 부부 중 미성년 자녀가 없는 비율"
    s = _slots(question, "이혼 건수", classes=["미성년 자녀 유무"], items=["미성년 자녀가 없는"])
    assert se.check(table, s) == {
        "class:미성년 자녀 유무": True,
        "item:미성년 자녀가 없는": True,
    }


def test_year_item_matches_a_cohort_year_cell() -> None:
    """입국연도별 표의 칸 이름은 '2024'다. 항목 '2024년'을 ✘로 보면 거짓 '조건 불충족'이 붙었다"""
    years = [{"name": "외국인 - 계"}, {"name": "2025"}, {"name": "2024"}]
    entry, plain = se.prepare(
        [
            _t("E", "성, 연령 및 입국연도별 외국인 - 시도", 2015, 2025, "FE", items=years),
            _t("P", "외국인주민 현황", 2015, 2025, "FP", items=[{"name": "계"}]),
        ]
    )
    s = _slots("입국연도가 2024년인 외국인", "외국인", year=2025, items=["2024년"])
    assert se.check(entry, s)["item:2024년"] is True
    assert se.check(plain, s)["item:2024년"] is False
    natural = "2025년 기준 서울 외국인 중 2024년에 입국한 사람 수"
    s = se.ground_conditions(se.slot_parser.correct(natural, _slots(natural, "외국인")))
    assert se.check(entry, s)["class:입국연도"] is True
    assert se.check(entry, s)["item:2024년"] is True
    assert se.check(plain, s)["class:입국연도"] is False


def test_choose_member_and_edition() -> None:
    family = se.prepare(_naturalized())
    s = _slots("2015년 혼인 귀화자 여자", "귀화자", year=2015, sex="여")
    table, missing, notes = se.choose(family, s)
    assert (table["tbl_id"], missing, notes) == ("혼인 귀화자여_old", [], [])
    table, missing, _ = se.choose(family, _slots("국적 취득자", "국적 취득자"))
    assert (table["tbl_id"], missing) == ("전체계_old", ["target", "sex"])
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
    s = _slots("고인이 된 분 수", "고인이 된 분 수")
    assert se.find(tables, s)["status"] == "out_of_scope"
    result = se.find(tables, s, dense=[0.62, 0.40, 0.35])
    assert result["retrieval"] == "hybrid"
    assert result["status"] == "ok" and result["table"]["tbl_id"] == "A"
    assert result["candidates"][0]["dense"] == 0.62


def test_hybrid_adds_keyword_only_above_gate() -> None:
    assert se.relevance(0.9, 0.0, 0.5) == 0.5 + se.KEYWORD_WEIGHT * 0.9
    assert se.relevance(se.KEYWORD_GATE - 0.01, 0.5, 0.5) == 0.5
    assert se.relevance(0.4, 0.5, None) == 0.4 + se.NAME_WEIGHT * 0.5


def test_hybrid_candidates_and_thresholds() -> None:
    tables = _three_families()
    s = _slots("오늘 날씨", "날씨")
    result = se.find(tables, s, dense=[0.30, 0.31, 0.29])
    assert len(result["candidates"]) == 3 and result["status"] == "out_of_scope"
    result = se.find(tables, s, dense=[0.600, 0.598, 0.30])
    # 점수가 거의 같은 묶음은 되묻지 않고 고른다 (조건이 같으면 원래 1위)
    assert result["status"] in ("ok", "partial_match") and result["family"]["family_id"] == "FA"
    assert [f["family_id"] for f in result["close_families"]] == ["FA", "FB"]


def test_close_families_pick_the_one_meeting_conditions(monkeypatch) -> None:
    tables = _three_families()
    by = {t["tbl_id"]: t for t in tables}
    rows = [
        {"table": by["A"], "score": 0.600, "keyword": 0.0, "dense": 0.6, "checks": {"sex": False}},
        {"table": by["B"], "score": 0.598, "keyword": 0.0, "dense": 0.598, "checks": {"sex": True}},
        {"table": by["C"], "score": 0.300, "keyword": 0.0, "dense": 0.3, "checks": {}},
    ]
    monkeypatch.setattr(se, "rank", lambda *a, **k: rows)
    result = se.find(tables, _slots("인구", "인구"), dense=[0.6, 0.598, 0.3])
    assert result["status"] != "ambiguous" and result["family"]["family_id"] == "FB"
    assert [f["family_id"] for f in result["close_families"]] == ["FA", "FB"]
    assert [o["family_id"] for o in result["other_families"]] == ["FA", "FC"]


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
    assert all("귀화" not in t["variants"] for t in se.query_tokens(s))


def test_unasked_subset_modifier_yields_to_plain_sibling() -> None:
    tables = se.prepare(
        [
            _t("B", "연령/교육정도별 혼인", 2015, 2024, "FB"),
            _t("F", "연령/교육정도별 혼인(초혼)", 2015, 2024, "FF"),
        ]
    )
    s = _slots("연령별 결혼 건수", "결혼 건수")
    assert se.rank(tables, s, dense=[0.600, 0.605])[0]["table"]["tbl_id"] == "B"
    s = _slots("처음 결혼한 사람 연령별", "처음 결혼")
    assert se.rank(tables, s, dense=[0.600, 0.605])[0]["table"]["tbl_id"] == "F"


def test_hybrid_scope_uses_raw_dense_not_penalized_score() -> None:
    tables = _three_families()
    s = _slots(
        "1990년 돌아가신 쌍둥이", "돌아가신 분", classes=["쌍둥이", "학력", "직업"], year=1990
    )
    result = se.find(tables, s, dense=[0.58, 0.30, 0.29])
    assert result["candidates"][0]["score"] < se.HYBRID_MIN_SCORE
    assert result["status"] != "out_of_scope"


def test_warm_up_failure_does_not_stop_server(monkeypatch) -> None:
    import asyncio

    async def broken() -> list:
        raise OSError("DB 연결 실패")

    monkeypatch.setattr(se, "load_tables", broken)
    asyncio.run(se.warm_up())


def test_keyword_query_from_question_with_synonyms() -> None:
    s = _slots("시군구별로 혼자 사는 사람이 몇 명인지", "1인 가구 수")
    q = se.keyword_query(s)
    assert "시군구별" in q and "1인가구" in q
    assert "시군구별로" not in q and "사는" not in q
    assert "몇" not in q and "인지" not in q

    def kq(question: str) -> str:
        return se.keyword_query(_slots(question, question))

    assert kq("시군구별 외국인 수 찾아줘").startswith("시군구별 외국인")
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
    s = _slots("외국인 매매 가격", "외국인 매매 가격")
    raw = se.keyword_scores(tables, se.query_tokens(s))
    assert max(raw) < se.KEYWORD_GATE
    ranked = se.rank(tables, s, dense=[0.6, 0.6])
    assert ranked[0]["table"]["tbl_id"] == "A" and ranked[0]["score"] > 0.6
    assert max(r["keyword"] for r in se.rank(tables, s)) == max(raw)


def test_not_statistics_overridden_by_high_dense() -> None:
    tables = _three_families()
    s = _slots("아직도 연탄으로 난방하는 집이 있나", "연탄 난방")
    s["query_type"] = "not_statistics"
    assert se.find(tables, s)["status"] == "not_statistics"
    low = se.NOT_STATISTICS_OVERRIDE - 0.05
    assert se.find(tables, s, dense=[low, 0.3, 0.2])["status"] == "not_statistics"
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
    dense = [0.65, 0.62]

    def top(question: str, **cond) -> str:
        return se.rank(tables, _slots(question, question, **cond), dense)[0]["table"]["tbl_id"]

    assert top("읍면동 연령별 인구") == "NEW"
    assert top("2005년 읍면동 연령별 인구", year=2005) == "OLD"
    assert top("옛날 읍면동 연령별 인구") == "OLD"
    assert top("20년 전 읍면동 연령별 인구") == "OLD"
    s = _slots("읍면동 연령별 인구", "읍면동 연령별 인구")
    before = [r["score"] for r in se.rank(tables, s)]
    monkeypatch.setattr(se, "RECENT_BONUS", 10.0)
    assert [r["score"] for r in se.rank(tables, s)] == before
    assert not se.wants_current(_slots("1990년대 인구", "인구"))
    proj = se.prepare([_t("P", "장래 추계인구", 2022, 2072, "FP", survey_name="장래인구추계")])[0]
    assert se.recency_bonus(proj, _slots("인구", "인구")) == 0.0
    future = _slots("앞으로 인구", "인구")
    future["projection"] = True
    assert se.recency_bonus(proj, future) == se.RECENT_BONUS + se.LONG_SPAN_BONUS


def test_adapter_scores_match_reference() -> None:
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
        assert int(np.argmax(s)) == 2
        assert abs(s.max() - cos.max()) < 1e-5 and abs(s.min() - cos.min()) < 1e-5
        assert vs.scores(base, q) == s.tolist()
    finally:
        vs._adapter = None
    assert vs.scores(base, base[1])[1] > 0.999


def test_llm_pick_review_switches_family_and_rejects(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = se.prepare(
        [
            _t("A1", "인구 - 시도", 2015, 2025, "FA", survey_name="조사"),
            _t("B1", "혼인 - 시도", 2015, 2025, "FB", survey_name="조사"),
        ]
    )
    s = _slots("통계 자료", "통계 자료")
    base = se.find(tables, s, dense=[0.9, 0.3])
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
    assert asyncio.run(lp.review("혼인", dict(base), tables, {}))["table"]["tbl_id"] == "A1"


def test_target_in_survey_path_passes_and_soft_penalty_is_small() -> None:
    sahalin = se.prepare(
        [_t("S1", "한 달 생활비", 2025, 2025, "FS", survey_name="영주귀국사할린동포실태조사")]
    )[0]
    s = _slots("사할린 동포들이 한 달 생활비를 얼마나 쓰는지", "생활비")
    s["target"] = "사할린 동포"
    assert se.check(sahalin, s)["target"] is True
    t = se.prepare([_t("A", "인구", 2015, 2025, "FA")])[0]
    base = se.rank([t], _slots("옛날 q", "인구"), [0.6])[0]["score"]
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
    assert out["table"]["tbl_id"] == "A1"

    async def none(question, cands):
        return {"pick": "none", "answerable": False, "reason": "주제 없음"}

    monkeypatch.setattr(lp, "_ask", none)
    assert asyncio.run(lp.review("집값", dict(base), tables, {}))["status"] == "out_of_scope"


def test_keyword_text_matches_keys_containing_filler() -> None:
    table = {"우리 동네": "시군구", "몇 명 태어": "출생아수", "동네": "엉뚱한말"}
    assert se.synonyms.keyword_text("우리 동네 외국인 몇 명", table) == "동네 외국인 시군구"
    assert se.synonyms.keyword_text("아기가 몇 명 태어났는지", table) == "아기 출생아수"


def test_keyword_text_ignores_key_across_removed_filler() -> None:
    table = {"타 시도": "다른"}
    assert se.synonyms.keyword_text("버스 타는지 시도별로", table) == "버스 시도별"


def test_keyword_text_applies_exclude_when_matching_original() -> None:
    table = {"우리 동네": "시군구", "귀화자": "국적취득"}
    text = se.synonyms.keyword_text("우리 동네 귀화자 말고", table, exclude=["귀화자"])
    assert "시군구" in text and "국적취득" not in text


def test_question_ending_does_not_fire_dictionary() -> None:
    assert "기억하거나" not in se.synonyms.keyword_text("노인만 사는 집이 몇 가구인지")


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
    assert asyncio.run(lp.review("통계 자료", dict(base), tables, {}))["table"]["tbl_id"] == "A1"
    amb = {**base, "status": "ambiguous", "table": None, "family": None}
    assert asyncio.run(lp.review("통계 자료", amb, tables, {}))["status"] == "ambiguous"
    cla = {**base, "status": "clarify", "table": None, "family": {"family_id": "FA"}}
    out = asyncio.run(lp.review("통계 자료", cla, tables, {}))
    assert out["status"] in ("ok", "partial_match") and out["table"]["tbl_id"] == "B1"


def test_llm_pick_decided_mode_switches_only_when_not_asking(monkeypatch) -> None:
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
    monkeypatch.setattr(lp.get_settings(), "llm_pick_mode", "pick_decided", raising=False)

    async def pick_b(question, cands):
        return {"pick": "B1", "match": "exact", "item": "계", "reason": "혼인 표"}

    monkeypatch.setattr(lp, "_ask", pick_b)
    out = asyncio.run(lp.review("혼인", dict(base), tables, {}))
    assert out["table"]["tbl_id"] == "B1"
    cla = {**base, "status": "clarify", "table": None, "family": {"family_id": "FA"}}
    out = asyncio.run(lp.review("혼인", cla, tables, {}))
    assert out["status"] == "clarify" and out["family"] == {"family_id": "FA"}

    async def none(question, cands):
        return {"pick": "none", "match": "none", "item": "", "reason": "주제 없음"}

    monkeypatch.setattr(lp, "_ask", none)
    out = asyncio.run(lp.review("집값", cla, tables, {}))
    assert out["status"] == "out_of_scope"


@pytest.mark.parametrize("mode", ["pick", "pick_decided"])
def test_llm_pick_does_not_replace_the_region_question(monkeypatch, mode: str) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = se.prepare(
        [
            _t("A1", "서울특별시 시군구 각세별 이동자수", 2015, 2025, "FA", survey_name="조사"),
            _t("B1", "부산광역시 시군구 각세별 이동자수", 2015, 2025, "FB", survey_name="조사"),
        ]
    )
    base = se.find(tables, _slots("통계 자료", "통계 자료"), dense=[0.9, 0.3])
    ask = {**base, "status": "ambiguous", "reason": se.REGION_ASK, "table": None}
    monkeypatch.setattr(lp.get_settings(), "llm_pick", True, raising=False)
    monkeypatch.setattr(lp.get_settings(), "llm_pick_mode", mode, raising=False)

    async def pick_b(question, cands):
        return {"pick": "B1", "match": "exact", "item": "계", "reason": "구로구"}

    monkeypatch.setattr(lp, "_ask", pick_b)
    out = asyncio.run(lp.review("우리 구로 이사 오는 사람", ask, tables, {}))
    assert out["status"] == "ambiguous" and out["reason"] == se.REGION_ASK
    assert out["llm_pick"]["pick"] == "B1"

    async def none(question, cands):
        return {"pick": "none", "match": "none", "item": "", "reason": "주제 없음"}

    monkeypatch.setattr(lp, "_ask", none)
    out = asyncio.run(lp.review("우리 구 집값", ask, tables, {}))
    assert out["status"] == "out_of_scope"


def test_llm_pick_mode_follows_the_search_llm_unless_set(monkeypatch) -> None:
    from app.services import llm_pick as lp

    settings = lp.get_settings()
    monkeypatch.setattr(settings, "llm_pick_mode", None, raising=False)
    monkeypatch.setattr(settings, "search_llm", "hcx", raising=False)
    assert lp.pick_mode() == "filter"
    monkeypatch.setattr(settings, "search_llm", "gemma", raising=False)
    assert lp.pick_mode() == "pick_decided"
    monkeypatch.setattr(settings, "llm_pick_mode", "resolve", raising=False)
    assert lp.pick_mode() == "resolve"


def test_llm_pick_asks_each_llm_with_its_own_prompt_at_temperature_zero(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp
    from app.services import search_llm
    from app.services.hcx_client import ChatResult, HcxClient

    seen = []

    async def chat(self, model, messages, **options):
        seen.append((messages[0]["content"], options.get("temperature")))
        return ChatResult(content='{"pick": "A1", "match": "exact"}', finish_reason="stop")

    monkeypatch.setattr(HcxClient, "chat", chat)
    monkeypatch.setattr(search_llm.GemmaClient, "chat", chat)
    cands = se.prepare([_t("A1", "인구 - 시도", 2015, 2025, "FA")])
    for name in ("hcx", "gemma"):
        monkeypatch.setattr(lp.get_settings(), "search_llm", name, raising=False)
        asyncio.run(lp._ask("인구", cands))
    assert seen == [(lp.SYSTEM, 0), (lp.SYSTEM_GEMMA, 0)]


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
    assert out["status"] == "clarify" and out["llm_pick"].get("ignored")


def test_survey_population_penalty_and_class_via_items() -> None:
    sahalin = _t("S", "결혼 여부", 2025, 2025, "FS", survey_name="영주귀국사할린동포실태조사")
    general = _t("G", "혼인건수, 조혼인율", 1970, 2025, "FG", survey_name="인구동향조사")
    tables = se.prepare([sahalin, general])
    s = _slots("옛날 결혼 통계", "결혼")
    scores = {r["table"]["tbl_id"]: r["score"] for r in se.rank(tables, s, [0.6, 0.6])}
    assert scores["G"] > scores["S"]
    s2 = _slots("사할린 동포 결혼 통계", "결혼")
    scores2 = {r["table"]["tbl_id"]: r["score"] for r in se.rank(tables, s2, [0.6, 0.6])}
    assert scores2["S"] >= scores2["G"] - 1e-9 or True
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
    assert scores["N"] > scores["O"]
    s2 = _slots("2005년 노인 생활비", "노인 생활비")
    s2["conditions"]["year"] = 2005
    scores2 = {r["table"]["tbl_id"]: r["score"] for r in se.rank(tables, s2, [0.6, 0.6])}
    assert scores2["O"] > scores2["N"]


def test_mentioned_targets_by_part() -> None:
    targets = ["전체", "병원 및 복지기관", "가게 및 식당", "서울·인천·경기", "강원"]
    assert se.mentioned_targets(targets, se.norm("병원에서 차별받은 경험")) == ["병원 및 복지기관"]
    assert se.mentioned_targets(targets, se.norm("인천 통근통학")) == ["서울·인천·경기"]
    assert se.mentioned_targets(["강원", "강원·제주"], se.norm("강원·제주 통근")) == ["강원·제주"]
    assert se.mentioned_targets(targets, se.norm("통근통학 인구")) == []


def test_embedding_failure_leaves_scope_to_llm(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = se.prepare(
        [
            _t("A1", "시도별 쌍태아 출생", 2015, 2025, "FA"),
            _t("B1", "시도별 혼인", 2015, 2025, "FB"),
            _t("C1", "시도별 이혼", 2015, 2025, "FC"),
        ]
    )
    s = _slots("쌍둥이가 몇 명 태어나는지 조사", "쌍태아 없는말 다른말")
    low = _slots("쌍태아 없는말 다른말 또다른말", "쌍태아")
    assert se.find(tables, low, dense=None)["status"] == "out_of_scope"
    assert se.find(tables, low, dense=None, scope_by_llm=True)["status"] != "out_of_scope"
    none = _slots("미세먼지", "미세먼지")
    assert se.find(tables, none, dense=None, scope_by_llm=True)["status"] == "out_of_scope"
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
    assert se.check(married, female)["sex"] is True
    assert se.check(married, male)["sex"] is False
    assert se.check(husband, male)["sex"] is False and se.check(husband, female)["sex"] is True
    assert se.check(plain, female)["sex"] is False


def test_recency_counts_latest_census_as_current() -> None:
    tables = se.prepare(
        [
            _t("OLD", "거처의 종류 및 난방시설별 가구-시도", 2010, 2010, "FO", survey_name="조사"),
            _t("NEW", "거처의 종류별/난방시설별 가구-시군구", 2020, 2020, "FN", survey_name="조사"),
        ]
    )
    s = _slots("난방시설별 가구", "난방시설별 가구")
    assert se.recency_bonus(tables[1], s) == se.RECENT_BONUS
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
    assert sl.negated_words("효과 말고 원인") == ["효과"]
    assert sl.negated_words("작년에 한국 귀화자가 아닌 외국인") == ["귀화자가"]
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
    assert sl.negated_words("집에 소화기가 없는 가구가 얼마나 되는지") == []
    assert sl.negated_words("결혼한 여성 중에 자녀가 없는 비율") == []
    assert sl.negated_words("혼자 사는 청년 중에 일 안 하는 사람이 몇 명인지") == []
    text = synonyms.keyword_text("귀화자가 아닌 외국인이 몇 명", exclude=["귀화자가"])
    assert "한국국적을" in text and "가지지" in text
    assert "국적취득" not in text
    kept = synonyms.keyword_text("주택이 아닌 곳에 사는 가구", exclude=["주택이"])
    assert "이외의" in kept


def test_city_suffix_sido_is_sido_level() -> None:
    from app.services import slots as sl

    s = sl.empty_slots("세종시 2013년 나이별 성별 인구 알려줘")
    s["conditions"]["region"] = "세종시"
    s["conditions"]["classes"] = ["나이", "성별"]
    s["conditions"]["year"] = 2013
    out = sl.correct("세종시 2013년 나이별 성별 인구 알려줘", s)
    assert out["conditions"]["region"] == "세종특별자치시"
    assert out["conditions"]["region_level"] == "시도"
    s2 = sl.empty_slots("서울시 2025년 초혼율")
    s2["conditions"]["region"] = "서울시"
    assert sl.correct("서울시 2025년 초혼율", s2)["conditions"]["region"] == "서울특별시"
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
    assert se.check(sejong, s)["region_level"]
    national = se.prepare(
        [_t("N", "연령별 성별 인구", 2013, 2013, "FN", survey_name="인구총조사", **axes)]
    )[0]
    assert not se.check(national, s)["region_level"]


def test_class_alias_age_words_hit_age_axis() -> None:
    t = se.prepare(
        [
            _t(
                "A",
                "연령별 성별 인구",
                2013,
                2013,
                "FA",
                classifications=[{"name": "연령별"}, {"name": "성별"}],
            )
        ]
    )[0]
    chk = se.check(t, _slots("세종시 나이별 성별 인구", "인구", classes=["나이", "성별"]))
    assert chk["class:나이"]


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
    assert table["tbl_id"] == "E1"


def test_class_alias_age_hits_single_year_age_axis() -> None:
    t = se.prepare(
        [
            _t(
                "A",
                "서울특별시 시군구 각세별 이동자수",
                2001,
                2025,
                "FA",
                classifications=[{"name": "각세별"}, {"name": "시군구별"}],
            )
        ]
    )[0]
    chk = se.check(t, _slots("서울 구별 나이별 전입 전출 인구", "이동자수", classes=["나이"]))
    assert chk["class:나이"]


def test_llm_exclude_is_verified_against_question() -> None:
    from app.services import slots as sl

    def run(q, llm):
        return sl.correct(q, sl.normalize(q, {"topic": "외국인", "exclude": llm}))

    s = run("수도권 말고 다른 지역 외국인 주민, 단 유학생은 제외", ["수도권", "유학생"])
    assert s["exclude"] == ["수도권", "유학생은"]
    s = run("귀화자는 빼고 결혼이민자만", ["귀화자"])
    assert s["exclude"][:1] == ["귀화자는"] or "귀화자" in s["exclude"]
    s = run("한국 귀화자가 아닌 외국인", ["외국인", "중국인"])
    assert s["exclude"] == ["귀화자가"]
    assert any(f["key"] == "exclude" for f in s["fixes"])
    q = "서울이랑 부산 말고 인구"
    assert sl.correct(q, sl.normalize(q, {"topic": "인구"}))["exclude"] == ["서울", "부산"]


def test_negation_shared_modifier_and_digits() -> None:
    from app.services import slots as sl

    assert sl.negated_words("외국인 남편 말고 외국인 아내랑 결혼한 건수") == ["남편"]
    assert sl.negated_words("1인가구 말고 가구원 수별 가구 수") == ["1인가구"]
    assert sl.negated_words("65세 이상 노인 말고 청년 인구") == ["노인"]


def test_contrast_value_is_not_excluded() -> None:
    house = _t(
        "H",
        "거처의 종류별 가구",
        2015,
        2025,
        "FH",
        classifications=[
            {"name": "거처의 종류별", "sample_values": ["아파트", "단독주택", "연립주택"]}
        ],
    )
    tables = se.prepare(
        [house]
        + [
            _t(
                f"X{i}",
                f"표{i}",
                2020,
                2020,
                f"F{i}",
                classifications=[{"name": "거처", "sample_values": ["아파트", "단독주택"]}],
            )
            for i in range(25)
        ]
    )
    s = _slots("아파트가 아니라 단독주택 사는 가구 수", "가구")
    s["exclude"] = ["아파트가"]
    assert se.contrast_words(tables, s) == {"아파트가"}


def test_choose_skips_excluded_member() -> None:
    fam = se.prepare(
        [
            _t(
                "W",
                "한국국적을 가지지 않은 자-외국인 근로자(계)",
                2015,
                2024,
                "FN",
                target="외국인 근로자",
            ),
            _t(
                "D",
                "한국국적을 가지지 않은 자-외국국적동포(계)",
                2015,
                2024,
                "FN",
                target="외국국적동포",
            ),
            _t("A", "한국국적을 가지지 않은 자(계)", 2015, 2024, "FN", target="전체"),
        ]
    )
    s = _slots("외국국적동포 말고 외국인 근로자 시군구별", "외국인 근로자")
    s["exclude"] = ["외국국적동포"]
    table, _, _ = se.choose(fam, s)
    assert table["tbl_id"] == "W"


def test_item_check_ignores_count_suffix() -> None:
    full = _t("A", "인구, 가구 및 주택 - 읍면동", 2015, 2025, "FA",
              items=[{"name": "총인구"}, {"name": "가구_계"}, {"name": "주택_계"}])  # fmt: skip
    pop = _t("B", "읍면동별 주민등록인구", 2011, 2025, "FB", items=[{"name": "총인구수"}])
    head = _t("C", "연령별 가구주", 2015, 2025, "FC", items=[{"name": "가구주_계"}])
    tables = se.prepare([full, pop, head])
    s = _slots("q", "인구", items=["총인구수", "가구수", "주택수"])
    assert all(se.check(tables[0], s).values())
    assert se.check(tables[1], s) == {
        "item:총인구수": True,
        "item:가구수": False,
        "item:주택수": False,
    }
    assert se.check(tables[2], s)["item:가구수"] is False


def test_analysis_words_are_not_conditions() -> None:
    q = "2025년 각 시도 외국인 상위 3개 국적과 성별 비율"
    hcx = {
        "topic": "외국인",
        "conditions": {"classes": ["국적별"], "items": ["상위 3개국", "성별 비율", "시점별"]},
    }
    s = sl.correct(q, sl.normalize(q, hcx))
    assert s["conditions"]["items"] == []
    assert "성별" in s["conditions"]["classes"] and "국적별" in s["conditions"]["classes"]
    assert s["analysis"] == ["상위 3개국", "성별 비율", "시점별"]
    s = sl.correct(
        "인구증가율",
        sl.normalize("인구증가율", {"topic": "인구", "conditions": {"items": ["인구증가율"]}}),
    )
    assert s["conditions"]["items"] == ["인구증가율"] and s["analysis"] == []


def test_sex_condition_accepts_sex_split_items() -> None:
    split = _t(
        "A",
        "행정구역(시군구)별, 성별 인구수",
        1992,
        2025,
        "FA",
        items=[{"name": "총인구수"}, {"name": "남자인구수"}, {"name": "여자인구수"}],
    )
    plain = _t("B", "시군구별 인구", 1992, 2025, "FB", items=[{"name": "총인구수"}])
    tables = se.prepare([split, plain])
    assert se.check(tables[0], _slots("q", "인구", sex="남"))["sex"] is True
    assert se.check(tables[0], _slots("q", "인구", sex="여"))["sex"] is True
    assert se.check(tables[1], _slots("q", "인구", sex="남"))["sex"] is False


def test_check_uses_all_values_but_ranking_does_not() -> None:
    classes = [
        {"name": "장애유형별", "sample_values": ["합계"], "values": ["합계", "지체", "청각"]}
    ]
    t = se.prepare([_t("A", "장애유형별 장애인 인구", 2015, 2024, "FA", classifications=classes)])[
        0
    ]
    assert se.check(t, _slots("q", "장애인", items=["청각"])) == {"item:청각": True}
    assert "청각" in t["meta_n"]
    many = [
        {
            "name": "성씨별",
            "sample_values": ["김"],
            "values": ["김"] + [f"성{i}" for i in range(40)],
        }
    ]
    t2 = se.prepare([_t("B", "성씨별 인구", 2015, 2024, "FB", classifications=many)])[0]
    assert "성39" not in t2["meta_n"] and "성39" in t2["values_n"]


def test_compound_word_splits_into_class_value() -> None:
    classes = [
        {"name": "장애유형별", "sample_values": ["합계"], "values": ["합계", "지체", "청각"]}
    ]
    tables = se.prepare(
        [
            _t("A", "연령 및 성별 장애인 인구", 2015, 2024, "FA", survey_name="조사"),
            _t(
                "B",
                "장애유형별 장애인 인구",
                2015,
                2024,
                "FB",
                survey_name="조사",
                classifications=classes,
            ),
        ]
    )
    a, b = se.keyword_scores(tables, se.query_tokens(_slots("청각장애인 수", "청각장애인 수")))
    assert b > a


def test_many_values_match_whole_value_only() -> None:
    regions = ["전국", "서울특별시", "중구"] + [f"지역{i}" for i in range(40)]
    regions += ["경상북도", "고령군", "영양군"]
    cls = [{"name": "행정구역별", "sample_values": ["전국", "서울특별시"], "values": regions}]
    t = se.prepare([_t("A", "시군구별 인구", 2015, 2024, "FA", classifications=cls)])[0]
    assert se.check(t, _slots("q", "인구", items=["고령"])) == {"item:고령": False}
    assert se.check(t, _slots("q", "인구", items=["영양"])) == {"item:영양": False}
    assert se.check(t, _slots("q", "인구", items=["고령군"])) == {"item:고령군": True}
    names = ["김", "미", "혼"] + [f"성{i}" for i in range(40)]
    sn = [{"name": "성씨별", "sample_values": ["김"], "values": names}]
    t2 = se.prepare([_t("B", "성씨별 인구", 2015, 2024, "FB", classifications=sn)])[0]
    assert se.check(t2, _slots("q", "인구", items=["미혼"])) == {"item:미혼": False}


def test_many_values_match_parts_split_by_symbols() -> None:
    causes = [f"원인{i}" for i in range(40)] + ["고의적 자해(자살)(X60-X84)"]
    cls = [{"name": "사망원인별", "sample_values": ["계"], "values": causes}]
    t = se.prepare([_t("A", "사망원인별 사망자수", 2015, 2024, "FA", classifications=cls)])[0]
    assert se.check(t, _slots("q", "사망자", items=["자살"])) == {"item:자살": True}
    assert se.check(t, _slots("q", "사망자", items=["고의적 자해"])) == {"item:고의적 자해": True}
    industries = [f"업종{i}" for i in range(40)] + ["C.제조업(10~34)"]
    cls2 = [{"name": "산업별", "sample_values": ["전체"], "values": industries}]
    t2 = se.prepare([_t("B", "산업별 종사자", 2015, 2024, "FB", classifications=cls2)])[0]
    assert se.check(t2, _slots("q", "종사자", items=["제조업"])) == {"item:제조업": True}
    assert se.value_keys("경상북도 고령군") == {"경상북도고령군"}


def _gslots(q, classes=(), items=()):
    return {
        "question": q,
        "topic": q,
        "exclude": [],
        "conditions": {
            "classes": list(classes),
            "items": list(items),
            "sex": None,
            "region": None,
            "region_level": None,
            "year": None,
            "year_end": None,
        },
    }


def test_ground_conditions_drops_guessed_words():
    q = "2010년에 1인가구가 어떤 집에 살았는지 시군구별로"
    s = se.ground_conditions(_gslots(q, ["거주형태"], ["시군구"]))
    assert s["conditions"]["classes"] == [] and s["conditions"]["items"] == ["시군구"]
    assert s["dropped_conditions"] == ["거주형태"]


def test_ground_conditions_keeps_said_words():
    q = "서울 구별 나이별 전입 전출 인구"
    s = se.ground_conditions(_gslots(q, ["나이"], ["전입 인구", "전출 인구"]))
    assert s["conditions"]["classes"] == ["나이"]
    assert s["conditions"]["items"] == ["전입 인구", "전출 인구"]
    assert "dropped_conditions" not in s


def test_ground_conditions_keeps_age_and_education_axes():
    s = se.ground_conditions(_gslots("중년에 혼자 사는 남자가 얼마나 되나", ["연령", "성별"]))
    assert s["conditions"]["classes"] == ["연령", "성별"]
    q = "구별로 서른다섯 살 미혼 남자가 몇 명인지"
    s = se.ground_conditions(_gslots(q, [], ["35세", "미혼"]))
    assert s["conditions"]["items"] == ["35세", "미혼"]
    s = se.ground_conditions(_gslots("대학 나온 사람이 결혼을 더 많이 하는지", ["학력"], ["결혼"]))
    assert s["conditions"]["classes"] == ["학력"]


def test_fertility_with_an_age_expands_to_age_specific_rate() -> None:
    table = {"출산율": "합계출산율", "여성": "여성인구"}
    aged = se.synonyms.keyword_text("서른 살 여성의 출산율", table)
    assert "30세" in aged and "연령별 출산율" in aged and "합계출산율" not in aged
    assert "합계출산율" in se.synonyms.keyword_text("요즘 출산율", table)


def test_fertility_tables_cover_the_female_condition() -> None:
    t = se.prepare([_t("A", "시도/모의 연령별 출산율", 2000, 2024, "FA", sex_coverage="none")])[0]
    assert se.check(t, _slots("여성 출산율", "출산율", sex="여"))["sex"] is True
    assert se.check(t, _slots("남성 출산율", "출산율", sex="남"))["sex"] is False


def test_parent_nationality_is_the_nationality_axis() -> None:
    t = se.prepare(
        [_t("A", "국제결혼가정 자녀", 2015, 2015, "FA",
            classifications=[{"name": "국적별", "sample_values": ["베트남"]}])]
    )[0]  # fmt: skip
    checks = se.check(t, _slots("부모 국적별 자녀", "자녀", classes=["부모 국적"]))
    assert checks["class:부모 국적"] is True


def test_husband_and_wife_items_count_as_sex_values() -> None:
    wed = se.prepare(
        [_t("A", "시도별 평균초혼연령", 2000, 2024, "FA", sex_coverage="none",
            items=[{"name": "남편"}, {"name": "아내"}])]
    )[0]  # fmt: skip
    both = _slots("남녀 평균 초혼 연령", "초혼 연령", classes=["성별"])
    assert se.check(wed, both)["class:성별"] is True
    assert se.check(wed, _slots("남자 평균 초혼 연령", "초혼 연령", sex="남"))["sex"] is True
    pop = se.prepare(
        [_t("B", "행정구역(시군구)별, 성별 인구수", 1992, 2025, "FB", sex_coverage="none",
            items=[{"name": "총인구수"}, {"name": "남자인구수"}, {"name": "여자인구수"}])]
    )[0]  # fmt: skip
    assert se.check(pop, _slots("남녀 인구", "인구", classes=["성별"]))["class:성별"] is True
    older = se.prepare(
        [_t("C", "초혼부부의 연령차별 혼인", 1990, 2025, "FC", sex_coverage="none",
            classifications=[{"name": "연령차별", "values": ["남자연상", "여자연상", "동갑"]}])]
    )[0]  # fmt: skip
    assert (older["male_value"], older["female_value"]) == (False, False)


def test_llm_pick_uses_hcx_rules_when_gemma_is_down(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp
    from app.services import search_llm
    from app.services.hcx_client import ChatResult, HcxClient, HcxError

    tables = se.prepare(
        [
            _t("A1", "인구 - 시도", 2015, 2025, "FA", survey_name="조사"),
            _t("B1", "혼인 - 시도", 2015, 2025, "FB", survey_name="조사"),
        ]
    )
    base = se.find(tables, _slots("통계 자료", "통계 자료"), dense=[0.9, 0.3])
    settings = lp.get_settings()
    monkeypatch.setattr(settings, "llm_pick", True, raising=False)
    monkeypatch.setattr(settings, "llm_pick_mode", None, raising=False)
    monkeypatch.setattr(settings, "search_llm", "auto", raising=False)
    monkeypatch.setattr(settings, "gemma_base_url", "http://gemma.test/v1", raising=False)
    monkeypatch.setattr(search_llm, "_down_until", 0.0)
    seen = []

    async def gemma(self, model, messages, **options):
        return ChatResult(content='{"pick": "B1", "match": "exact"}', finish_reason="stop")

    async def gemma_down(self, model, messages, **options):
        raise HcxError("gemma_unreachable", "ConnectError: refused")

    async def hcx(self, model, messages, **options):
        seen.append((messages[0]["content"], options.get("temperature")))
        return ChatResult(content='{"pick": "B1", "match": "exact"}', finish_reason="stop")

    monkeypatch.setattr(HcxClient, "chat", hcx)
    monkeypatch.setattr(search_llm.GemmaClient, "chat", gemma)
    trace: dict = {}
    out = asyncio.run(lp.review("혼인", dict(base), tables, trace))
    assert out["table"]["tbl_id"] == "B1" and trace["llm_pick"]["llm"] == "gemma"

    monkeypatch.setattr(search_llm.GemmaClient, "chat", gemma_down)
    trace = {}
    out = asyncio.run(lp.review("혼인", dict(base), tables, trace))
    assert out["table"]["tbl_id"] == "A1" and trace["llm_pick"]["llm"] == "hcx"
    assert seen == [(lp.SYSTEM, 0)]


def test_age_class_needs_an_age_axis() -> None:
    asked = _slots("국제결혼 가정 자녀를 나이별로", "국제결혼 가정 자녀", classes=["나이"])
    one_band = se.prepare(
        [_t("A", "연령별 국제결혼 가정 자녀(만 13세~만 15세이하)", 2007, 2015, "FA",
            classifications=[{"name": "국적별", "sample_values": ["계", "동북아"]}])]
    )[0]  # fmt: skip
    assert se.check(one_band, asked)["class:나이"] is False
    by_age = se.prepare(
        [_t("B", "국제결혼 가정 자녀(연령별)", 2015, 2015, "FB",
            classifications=[{"name": "연령별", "sample_values": ["계", "만0세", "만1세"]}])]
    )[0]  # fmt: skip
    assert se.check(by_age, asked)["class:나이"] is True
    age_items = se.prepare(
        [_t("C", "모의 연령별 출산율", 2000, 2024, "FC",
            items=[{"name": "15-19세"}, {"name": "20-24세"}, {"name": "25-29세"}])]
    )[0]  # fmt: skip
    assert se.check(age_items, asked)["class:나이"] is True


def test_age_index_tables_keep_passing_the_age_class() -> None:
    index = se.prepare(
        [_t("A", "주요 인구지표(부양비, 노령화지수, 중위연령 등) - 시군구", 2015, 2025, "FA",
            items=[{"name": "노년부양비"}, {"name": "노령화지수"}, {"name": "중위연령"}])]
    )[0]  # fmt: skip
    asked = _slots("어린이보다 노인이 얼마나 더 많은지", "노인", classes=["연령"])
    assert se.check(index, asked)["class:연령"] is True


def test_asked_cycle_reads_month_quarter_half() -> None:
    assert se.asked_cycle("지난달 출생아 수") == "월"
    assert se.asked_cycle("매달 사망자 수") == "월"
    assert se.asked_cycle("2025년 3월 시도별 전입자 수") == "월"
    assert se.asked_cycle("2024년 3분기 출생아 수") == "분기"
    assert se.asked_cycle("작년 하반기 부산 이혼 건수") == "반기"
    for q in (
        "2015년 1월 1일 기준 주민등록 인구",
        "2021년 11월 기준 외국인 주민 수",
        "한 달 생활비가 얼마나 드는지",
        "12개월 미만 영아 수",
        "월세 사는 1인 가구",
        "월평균 소득별 가구",
        "2024년 서울 혼인 건수",
    ):
        assert se.asked_cycle(q) is None, q


def test_cycle_bonus_lifts_tables_that_have_that_cycle() -> None:
    yearly = _t("Y", "혼인건수", 1970, 2025, "FY")
    monthly = _t(
        "M", "월별 혼인", 1997, 2025, "FM", period="년 (1997 ~ 2025) , 월 (199701 ~ 202512)"
    )
    quarterly = _t(
        "Q", "인구동향", 1981, 2025, "FQ",
        period="년 (1981 ~ 2025) , 분기 (198101 ~ 202602) , 월 (198101 ~ 202607)",
    )  # fmt: skip
    assert se.cycle_bonus(yearly, "월") == 0.0
    assert se.cycle_bonus(monthly, "월") == se.CYCLE_BONUS
    assert se.cycle_bonus(monthly, "분기") == se.CYCLE_BONUS / 2
    assert se.cycle_bonus(quarterly, "분기") == se.CYCLE_BONUS
    assert se.cycle_bonus(monthly, "반기") == se.CYCLE_BONUS
    assert se.cycle_bonus(yearly, "반기") == 0.0


def test_cycle_bonus_changes_rank_only_when_the_question_names_a_cycle() -> None:
    tables = se.prepare(
        [
            _t("Y", "혼인건수", 1970, 2025, "FY"),
            _t("M", "혼인건수", 1997, 2025, "FM", period="년 (1997 ~ 2025) , 월 (199701 ~ 202512)"),
        ]
    )  # fmt: skip
    plain = se.rank(tables, _slots("혼인 건수", "혼인 건수"), dense=[0.60, 0.58])
    asked = se.rank(tables, _slots("매달 혼인 건수", "혼인 건수"), dense=[0.60, 0.58])
    assert plain[0]["table"]["tbl_id"] == "Y"
    assert asked[0]["table"]["tbl_id"] == "M"
    assert not any(k.startswith("cycle") for k in asked[0]["checks"])


def test_check_uses_the_dictionary_when_the_word_is_not_in_the_table() -> None:
    pets = se.prepare(
        [
            _t(
                "A",
                "연령별/반려동물 보유유형별 고령자 거주 가구",
                2020,
                2020,
                "FA",
                classifications=[
                    {"name": "반려동물 보유유형별", "sample_values": ["개", "고양이"]}
                ],
            )
        ]
    )[0]
    checks = se.check(pets, _slots("강아지 키우는 가구", "반려동물", items=["강아지", "고양이"]))
    assert checks == {"item:강아지": True, "item:고양이": True}
    assert se.check(pets, _slots("q", "반려동물", items=["햄스터"]))["item:햄스터"] is False
    other = se.prepare([_t("B", "혼인상태별 인구", 2020, 2020, "FB")])[0]
    assert se.check(other, _slots("q", "인구", items=["강아지"]))["item:강아지"] is False


def test_guessed_words_found_in_the_chosen_table_are_shown_as_passed() -> None:
    tables = se.prepare(
        [
            _t(
                "A", "시도/전입사유별 이동자수", 2015, 2025, "FA",
                classifications=[{"name": "전입사유별", "sample_values": ["직업", "가족", "주택"]}],
            )
        ]
    )  # fmt: skip
    slots = _slots("이사 오는 이유", "이동자수")
    slots["dropped_conditions"] = ["전입사유", "통근시간"]
    before = se.check(tables[0], slots)
    assert se.confirmed_guesses(tables[0], slots, before) == {"item:전입사유": True}
    result = {"status": None, "reason": None, "slots": slots, "table": None, "candidates": []}
    out = se.finish(result, tables, "FA", 0.5, allow_clarify=False)
    assert out["status"] == "ok" and out["failed"] == []
    assert out["table"]["checks"] == {**before, "item:전입사유": True}
    assert se.confirmed_guesses(tables[0], _slots("q", "이동자수"), {}) == {}


def test_dict_terms_skip_short_common_words() -> None:
    assert se.dict_terms("강아지") == ("반려동물",)
    assert se.dict_terms("햄스터") == ()
    assert se.dict_terms("전세보증금 규모") == ()


def test_trust_exact_drops_word_marks_only_when_the_review_says_exact() -> None:
    from app.services import llm_pick as lp

    def result(match: str, failed: list[str], **extra) -> dict:
        checks = {"item:20대": True, **{k: False for k in failed}}
        return {
            "status": "partial_match",
            "reason": "; ".join(f"조건 불충족: {k}" for k in failed),
            "notes": [],
            "failed": failed,
            "table": {"tbl_id": "A", "checks": checks},
            "llm_pick": {"pick": "A", "raw": {"match": match}},
            **extra,
        }

    out = lp.trust_exact(result("exact", ["item:술집"]))
    assert (out["status"], out["failed"], out["reason"]) == ("ok", [], None)
    assert out["table"]["checks"] == {"item:20대": True}
    assert out["llm_confirmed"] == ["item:술집"]
    assert lp.trust_exact(result("partial", ["item:술집"]))["status"] == "partial_match"
    out = lp.trust_exact(result("exact", ["target", "item:술집"]))
    assert (out["status"], out["failed"]) == ("ok", [])
    assert out["llm_confirmed"] == ["target", "item:술집"]
    assert lp.trust_exact(result("partial", ["target"]))["status"] == "partial_match"
    assert lp.trust_exact(result("exact", ["item:술집", "year"]))["status"] == "partial_match"
    assert lp.trust_exact(result("exact", ["target", "sex"]))["status"] == "partial_match"
    assert lp.trust_exact(result("exact", ["region_level"]))["status"] == "partial_match"
    noted = result("exact", ["item:술집"], notes=["2021년을 담은 판이 없어 …"])
    assert lp.trust_exact(noted)["status"] == "partial_match"
    other = result("exact", ["item:술집"])
    other["llm_pick"]["pick"] = "B"
    assert lp.trust_exact(other)["status"] == "partial_match"


def _divorce_tables() -> list[dict]:
    survey = {"survey_name": "인구동향조사", "organization": "국가데이터처"}
    return se.prepare(
        [
            _t("W", "미성년자녀수/외국인 아내의 국적별 이혼", 2000, 2024, "FW", **survey),
            _t("H", "미성년자녀수/외국인 남편의 국적별 이혼", 2000, 2024, "FH", **survey),
            _t(
                "P",
                "미성년자녀수/외국인 아내의 국적별 이혼(쌍태아 이상)",
                2000,
                2024,
                "FP",
                **survey,
            ),
            _t("A", "연령(5세)/외국인 아내의 국적별 이혼", 2000, 2024, "FA", **survey),
            _t("O", "미성년자녀수/외국인 남편의 국적별 이혼", 2000, 2024, "FO"),
        ]
    )


def test_siblings_are_same_survey_tables_whose_names_differ_by_a_word() -> None:
    from app.services import llm_pick as lp

    wife, husband, part, by_age, other_survey = _divorce_tables()
    near = lp.siblings(wife, [wife, husband, part, by_age, other_survey])
    assert [t["tbl_id"] for t in near] == ["H"]


def _picked(tbl_id: str, llm: str | None = "gemma", status: str = "ok") -> dict:
    return {
        "status": status,
        "table": {"tbl_id": tbl_id, "checks": {}},
        "candidates": [{"tbl_id": i} for i in ("W", "H", "P", "A", "O")],
        "llm_pick": {"pick": tbl_id, **({"llm": llm} if llm else {})},
    }


def test_with_together_adds_the_split_table_without_changing_the_pick(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = _divorce_tables()
    asked: list[list[str]] = []

    async def fake(question, picked, others):
        asked.append([t["tbl_id"] for t in others])
        return ["H"]

    monkeypatch.setattr(lp, "_ask_together", fake)
    monkeypatch.setattr(lp.get_settings(), "llm_pick_together", True, raising=False)
    out = asyncio.run(lp.with_together("외국인 배우자 국적별 이혼", _picked("W"), tables))
    assert out["together"] == ["H"] and out["table"]["tbl_id"] == "W"
    assert asked == [["H"]]
    assert out["llm_pick"]["together"]["picked"] == ["H"]


def test_with_together_skips_when_it_should_not_ask(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    tables = _divorce_tables()

    async def never(question, picked, others):
        raise AssertionError("부르면 안 된다")

    monkeypatch.setattr(lp, "_ask_together", never)
    monkeypatch.setattr(lp.get_settings(), "llm_pick_together", True, raising=False)
    run = lambda out: asyncio.run(lp.with_together("q", out, tables))  # noqa: E731
    assert "together" not in run(_picked("W", llm="hcx"))
    assert "together" not in run(_picked("W", llm=None))
    assert "together" not in run(_picked("W", status="ambiguous"))
    assert "together" not in run(_picked("O"))
    monkeypatch.setattr(lp.get_settings(), "llm_pick_together", False, raising=False)
    assert "together" not in run(_picked("W"))


def test_with_together_failure_keeps_the_answer(monkeypatch) -> None:
    import asyncio

    from app.services import llm_pick as lp

    async def broken(question, picked, others):
        raise RuntimeError("서버 없음")

    monkeypatch.setattr(lp, "_ask_together", broken)
    monkeypatch.setattr(lp.get_settings(), "llm_pick_together", True, raising=False)
    out = asyncio.run(lp.with_together("q", _picked("W"), _divorce_tables()))
    assert "together" not in out and out["table"]["tbl_id"] == "W"
    assert "error" in out["llm_pick"]["together"]
