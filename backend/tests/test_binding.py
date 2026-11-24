"""조건 → 칸 잇기 1단계 (app/services/binding.py): 기록만, 답은 그대로"""

import asyncio

import pytest

from app.core.config import get_settings
from app.services import binding
from app.services import dynamic_indicator as d

Dim = d.Dim


def _table(monkeypatch, dims, region_dim=None, name="표", years=(2000, 2025), couple=False):
    monkeypatch.setattr(d, "schema", lambda t: (dims, region_dim, "시도"))
    monkeypatch.setattr(
        binding.lookup,
        "_table",
        lambda t: {"tbl_nm": name, "start_year": years[0], "end_year": years[1]},
    )
    monkeypatch.setattr(d, "couple_counted", lambda t: couple)


def _slots(**cond):
    base = {"items": [], "classes": [], "sex": None, "region": None, "year": None}
    return {"conditions": {**base, **cond}}


def _by(b: binding.Binding, cond: str) -> binding.Bound:
    return next(x for x in b.bound if x.cond == cond)


def test_female_only_table_and_count_range(monkeypatch) -> None:
    # V2-034: 기혼여성 표는 성별 분류가 없어도 '여'를 만족하고, '셋 이상'은 출생자녀 3명 ~ 10명이상
    items = Dim(
        0,
        "항목",
        {"T0": "기혼여성(15세 이상)-계"} | {f"T{i}": f"출생자녀 {i}명" for i in range(1, 10)},
    )
    items.codes["T10"] = "출생자녀 10명이상"
    dead = Dim(2, "사망자녀수별", {"0": "계", "1": "0명", "2": "1명", "3": "2명", "4": "3명 이상"})
    _table(
        monkeypatch, [items, dead], name="사망자녀 수 및 출생자녀수별 기혼여성인구(15세 이상)-시도"
    )
    b = binding.bind(
        "T", "2010년에 애를 셋 이상 낳은 기혼 여성 다 합치면?", {"sex": "여", "year": 2010},
        _slots(items=["셋 이상"], sex="여"),
    )  # fmt: skip
    assert b.status == binding.BOUND
    assert _by(b, "sex").by == "table"
    item = _by(b, "item")
    assert item.codes == [f"T{i}" for i in range(3, 11)]
    assert _by(b, "사망자녀수별").codes == ["0"]  # 말하지 않은 분류는 합계


def test_exact_count_and_age_words_are_not_counts(monkeypatch) -> None:
    items = Dim(0, "항목", {"T10": "미성년 자녀가 있는 가구_계", "T20": "1자녀", "T21": "2자녀",
                            "T22": "3자녀", "T23": "4자녀", "T24": "5자녀 이상"})  # fmt: skip
    _table(monkeypatch, [items])
    b = binding.bind("T", "미성년 자녀가 딱 셋인 집", {}, _slots(items=["미성년 자녀가 딱 셋"]))
    assert _by(b, "item").codes == ["T22"]
    b = binding.bind("T", "자녀 넷 이상", {}, _slots(items=["자녀 넷 이상"]))
    assert _by(b, "item").codes == ["T23", "T24"]
    assert binding._count_ask("65살 이상") is None  # 나이는 수 조건이 아니다
    assert binding._count_ask("30세") is None


def test_age_cells_come_before_table_name_range(monkeypatch) -> None:
    # '고령자(65세이상) 가구' 표도 65세 이상 없는 가구까지 담는다: 표 이름만 보고 잇지 않는다
    names = ["일반가구_계", "가구원 모두 65세이상", "가구원 65세이상+65세미만"]
    names.append("가구원 65세이상 없음")
    items = Dim(0, "항목", {str(i): n for i, n in enumerate(names)})
    _table(monkeypatch, [items], name="고령자(65세이상) 가구(일반가구)")
    b = binding.bind("T", "식구가 전부 65살 이상인 집", {"ages": [[65, None]]}, _slots())
    assert b.status == binding.PARTIAL
    assert any(u.cond == "age" and "여럿" in u.why for u in b.unbound)
    # 그 연령이 든 칸이 없을 때만 표 이름의 연령 범위로 표 전체를 본다
    only = Dim(0, "항목", {"0": "가구", "1": "인구"})
    _table(monkeypatch, [only], name="고령자(65세이상) 가구")
    b = binding.bind("T", "65세 이상 가구", {"ages": [[65, None]]}, _slots())
    assert _by(b, "age").by == "table"


def test_sex_not_said_follows_the_same_order_as_choose(monkeypatch) -> None:
    items = Dim(0, "항목", {"T3": "혼인"})
    region = Dim(1, "시도별", {"00": "전국", "11": "서울특별시"})
    sex = Dim(2, "성별", {"1": "남자", "2": "여자"})
    age = Dim(3, "연령(각세)별", {"000": "계", "230": "30세", "231": "31세", "232": "32세"})
    _table(monkeypatch, [items, region, sex, age], region_dim=1, couple=True)
    b = binding.bind("T", "1975년 서울 혼인 건수", {"regions": ["서울"], "year": 1975}, _slots())
    assert _by(b, "성별").by == "couple" and _by(b, "성별").codes == ["1"]
    b = binding.bind("T", "서울 30대 혼인", {"ages": [[30, 39]]}, _slots())
    assert _by(b, "성별").by == "split" and _by(b, "성별").codes == ["1", "2"]
    _table(monkeypatch, [items, region, sex, age], region_dim=1, couple=False)
    b = binding.bind("T", "서울 기혼자 수", {}, _slots())
    assert _by(b, "성별").by == "split"


def test_item_in_table_name_and_missing_item(monkeypatch) -> None:
    items = Dim(0, "항목", {"1": "남편", "2": "아내"})
    _table(monkeypatch, [items], name="시도별 평균재혼연령")
    b = binding.bind("T", "재혼하는 남자 평균 나이", {"sex": "남"}, _slots(items=["재혼"]))
    assert _by(b, "item").by == "table" and _by(b, "sex").codes == ["1"]
    b = binding.bind("T", "부부 불화", {"sex": "남"}, _slots(items=["부부 사이가 안 좋아서"]))
    assert any(u.cond == "item" for u in b.unbound)


def test_year_outside_and_region_without_dimension(monkeypatch) -> None:
    items = Dim(0, "항목", {"1": "인구"})
    _table(monkeypatch, [items], years=(1970, 1980))
    b = binding.bind("T", "1990년 부산 인구", {"year": 1990, "regions": ["부산"]}, _slots())
    whys = {u.cond: u.why for u in b.unbound}
    assert "수록 기간 밖" in whys["year"] and whys["region"] == "표에 지역 구분이 없음"


def test_compare_with_current_plan(monkeypatch) -> None:
    items = Dim(0, "항목", {"1": "인구", "2": "가구"})
    _table(monkeypatch, [items])
    b = binding.bind("T", "인구", {}, _slots())
    same = d.Plan(pick={0: "1"})
    assert binding.compare(b, same, ok=True) == {"vs_current": binding.SAME}
    other = binding.compare(b, d.Plan(pick={0: "2"}), ok=True)
    assert other["vs_current"] == binding.DIFFER
    assert other["diff"] == [{"dim": "항목", "current": ["가구"], "binding": ["인구"]}]
    assert binding.compare(b, None, ok=False) == {"vs_current": binding.ONLY_BINDING}
    missing = binding.bind("T", "외국인", {}, _slots(items=["외국인"]))
    assert binding.compare(missing, None, ok=False) == {"vs_current": binding.BOTH_FAIL}
    assert binding.compare(missing, same, ok=True) == {"vs_current": binding.ONLY_CURRENT}


def test_shadow_never_breaks_the_answer(monkeypatch) -> None:
    def missing(t):
        raise d.StatsTableMissing(t)

    monkeypatch.setattr(d, "schema", missing)
    assert binding.shadow("T", "q", {}, None, None, False) is None

    def broken(t):
        raise ValueError("x")

    monkeypatch.setattr(d, "schema", broken)
    assert binding.shadow("T", "q", {}, None, None, False) == {"error": "잇기 실패"}


@pytest.mark.parametrize("on", [True, False])
def test_build_records_binding_only_when_switched_on(monkeypatch, on: bool) -> None:
    plan = d.Plan(pick={0: "1"})

    def fake_build(tbl_id, question, parsed, forced, slots, seen):
        seen["plan"] = plan
        return {"cells": "항목 인구"}, None

    got = {}

    def fake_shadow(tbl_id, question, parsed, slots, p, ok, forced=None):
        got.update(plan=p, ok=ok)
        return {"status": "bound", "vs_current": "같음"}

    monkeypatch.setattr(d, "build", fake_build)
    monkeypatch.setattr(binding, "shadow", fake_shadow)
    monkeypatch.setattr(get_settings(), "binding_shadow", on)
    step: dict = {}
    spec, why = asyncio.run(d._build("T", "101", "인구", {}, step))
    assert spec == {"cells": "항목 인구"} and why is None  # 답(정의)은 그대로
    if on:
        assert step["binding"]["vs_current"] == "같음" and got == {"plan": plan, "ok": True}
    else:
        assert "binding" not in step and not got


def test_compare_script_picks_the_answering_table(tmp_path) -> None:
    import json

    from scripts import binding_compare as bc

    ok_step = {"tbl_id": "A", "status": "ok", "binding": {"vs_current": "같음", "bound": []}}
    picked = {
        "tbl_id": "B",
        "kind": "picked",
        "skip": "표에 성별 구분이 없음",
        "binding": {
            "vs_current": "잇기만 성공",
            "bound": [{"cond": "sex", "value": "여", "by": "table"}],
        },
    }
    failed = {
        "tbl_id": "C",
        "kind": "picked",
        "binding": {
            "vs_current": "둘 다 실패",
            "unbound": [{"cond": "item", "value": "부부 불화", "why": "맞는 칸 없음"}],
        },
    }
    rows = [
        {"id": "D-1", "first": {"status": "ok", "trace": {"dynamic": [picked, ok_step]}}},
        {"id": "D-2", "first": {"status": "tables", "trace": {"dynamic": [picked]}}},
        {"id": "D-3", "first": {"status": "tables", "trace": {"dynamic": [failed]}}},
        {"id": "D-4", "first": {"status": "ok", "trace": {"dynamic": [{"tbl_id": "X"}]}}},
    ]
    path = tmp_path / "x.raw.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
    by = bc.collect(path)
    assert {k: [i for i, _ in v] for k, v in by.items()} == {
        "같음": ["D-1"],  # 답을 낸 표가 대표
        "잇기만 성공": ["D-2"],
        "둘 다 실패": ["D-3"],
    }
    text = "\n".join(bc.report(by, "x"))
    assert "| D-2 | B | 표에 성별 구분이 없음 | sex '여' → table |" in text
    assert "| D-3 | C | item '부부 불화' | 맞는 칸 없음 |" in text


def test_no_question_word_means_not_bound(monkeypatch) -> None:
    # '아이 키우기 좋은 동네': 말하지 않은 분류를 모두 합계로 채워도 질문과 잇는 말이 없으면 못 이음
    items = Dim(0, "항목", {"1": "계"})
    order = Dim(2, "출산순위별", {"0": "총계", "1": "첫째아"})
    _table(monkeypatch, [items, order], name="출산순위별 출생")
    b = binding.bind("T", "아이 키우기 좋은 동네 추천해 줘", {}, {"topic": "동네 추천"})
    assert b.status == binding.PARTIAL
    assert [u.cond for u in b.unbound] == ["topic"]
    # 주제 낱말이 표 이름에 있다는 것만으로는 잇지 않는다 (D-063)
    b = binding.bind("T", "출생 몇 명", {}, {"topic": "출생"})
    assert b.status == binding.PARTIAL


def test_husband_cell_only_for_couple_counted_tables(monkeypatch) -> None:
    # 평균 초혼연령(남편 · 아내 값이 다름)은 남편 칸 하나가 아니라 남녀 따로 (V2-116)
    items = Dim(0, "항목", {"T1": "남편", "T2": "아내"})
    _table(monkeypatch, [items], name="시도별 평균초혼연령", couple=False)
    b = binding.bind("T", "평균초혼연령", {}, _slots())
    assert _by(b, "항목").by == "split"
    _table(monkeypatch, [items], name="이혼", couple=True)
    b = binding.bind("T", "이혼 건수", {}, _slots())
    assert _by(b, "항목").by == "couple" and _by(b, "항목").codes == ["T1"]


def test_forced_cells_and_sex_from_parsed_only(monkeypatch) -> None:
    items = Dim(0, "항목", {"1": "인구", "2": "가구"})
    nation = Dim(2, "외국인 아내의 국적별", {"0": "계", "5": "베트남"})
    _table(monkeypatch, [items, nation])
    # 통계표 찾기의 성별 '여'(외국인 아내)는 보지 않는다 (V2-001)
    b = binding.bind("T", "베트남 여자랑 결혼", {"sex": ""}, _slots(sex="여"), forced={0: "1"})
    assert b.status == binding.BOUND
    assert _by(b, "forced").codes == ["1"] and _by(b, "word").codes == ["5"]


def test_blocked_after_choosing_cells_is_not_binding_only(monkeypatch) -> None:
    items = Dim(0, "항목", {"1": "쌍태아 (명)"})
    _table(monkeypatch, [items])
    b = binding.bind("T", "쌍둥이 몇 쌍", {}, _slots(), forced={0: "1"})
    got = binding.compare(b, d.Plan(pick={0: "1"}), ok=False)
    assert got == {"vs_current": binding.CHECK_BLOCKED}


def test_ratio_questions_are_not_compared(monkeypatch) -> None:
    items = Dim(0, "항목", {"1": "1인가구", "2": "일반가구"})
    _table(monkeypatch, [items])
    rec = binding.shadow("T", "1인 가구 비율", {}, _slots(), None, False)
    assert rec["vs_current"] == binding.NOT_COMPARED


def test_all_leaf_cells_mean_the_total(monkeypatch) -> None:
    # '출퇴근하거나 학교 다닌' = 통근 + 통학 → 통근·통학여부 계 (지금 계산의 choose와 같다, D-015)
    items = Dim(0, "항목", {"1": "전철 지하철", "2": "시내버스"})
    kind = Dim(2, "통근·통학여부별", {"0": "계", "1": "통근", "2": "통학"})
    _table(monkeypatch, [items, kind])
    q = "2010년에 지하철 타고 통근하거나 통학한 사람"
    b = binding.bind("T", q, {}, _slots(items=["지하철"]))
    assert _by(b, "item").codes == ["1"]
    assert _by(b, "word").by == "total" and _by(b, "word").codes == ["0"]


def test_short_cell_inside_long_condition_is_not_linked(monkeypatch) -> None:
    # '부부 및 미혼 형제자매' 안의 '미혼'은 혼인상태 칸이 아니다 (D-144)
    items = Dim(0, "항목", {"T12": "1세대가구-부부+미혼형제자매"})
    marry = Dim(3, "혼인상태별", {"0": "합계", "1": "미혼", "2": "유배우"})
    _table(monkeypatch, [items, marry])
    b = binding.bind(
        "T", "부부랑 미혼 형제자매가 같이 사는 집", {}, _slots(items=["부부 및 미혼 형제자매"])
    )
    assert not any(x.cond == "item" and x.dim == 3 for x in b.bound)
    # 조건의 낱말이 모두 든 칸이 하나뿐이면 그 칸 ('및'은 잇는 말)
    assert _by(b, "item").codes == ["T12"]
    assert binding._related("지하철", "전철 지하철")


def test_age_condition_missed_by_understanding_shows_as_differ(monkeypatch) -> None:
    # 질문 이해가 '85세 넘은'을 놓치고 통계표 찾기만 조건으로 뽑음: 표에 연령 분류가 있으면 잇고,
    # 연령 없이 계산한 지금 계산과는 '다름' (D-144)
    items = Dim(0, "항목", {"T12": "1세대가구-부부+미혼형제자매"})
    age = Dim(4, "연령별", {"000": "합계", "1": "80~84세", "2": "85세이상", "3": "20~24세"})
    _table(monkeypatch, [items, age])
    q = "85세 넘은 분 중에 부부랑 미혼 형제자매가 같이 사는 집"
    b = binding.bind("T", q, {}, _slots(items=["85세 이상"]), forced={0: "T12"})
    assert b.status == binding.BOUND and _by(b, "item").by == "age_range"
    got = binding.compare(b, d.Plan(pick={0: "T12"}), ok=True)
    assert got["vs_current"] == binding.DIFFER
    assert got["diff"] == [
        {"dim": "연령별", "current": ["연령 조건 없음"], "binding": ["85세 이상"]}
    ]


def test_item_already_in_forced_cell_and_age_endpoints(monkeypatch) -> None:
    items = Dim(0, "항목", {"T10": "내국인(15세이상)-계", "T14": "내국인-이혼", "T24": "남자-이혼"})
    age = Dim(2, "연령별", {"000": "합계", "1": "60~64세", "2": "65~69세", "3": "70~74세"})
    _table(monkeypatch, [items, age])
    # 칸 고르기 LLM이 고른 '내국인-이혼'이 '이혼' 조건을 담는다 (V2-016)
    b = binding.bind("T", "이혼한 상태인 사람", {}, _slots(items=["이혼"]), forced={0: "T14"})
    assert b.status == binding.BOUND and _by(b, "item").by == "forced"
    # '65살부터 72살까지'의 양 끝을 따로 뽑은 조건은 연령에서 이미 봤다 (V2-137)
    b = binding.bind("T", "65살부터 72살까지", {"ages": [[65, 72]]}, _slots(items=["65세", "72세"]))
    assert not any(u.cond == "item" for u in b.unbound)


def test_count_condition_skips_age_cells(monkeypatch) -> None:
    # '셋 이상'을 나이 칸 '3세 · 4세 · 5세'에 잇지 않는다 (DT_1YC1601)
    items = Dim(
        0, "항목", {"T13": "3자녀이상", "T24": "3세", "T25": "4세", "T26": "5세", "T27": "6세"}
    )
    _table(monkeypatch, [items])
    b = binding.bind("T", "자녀 셋 이상", {}, _slots(items=["셋 이상"]))
    assert not any(x.cond == "item" and set(x.codes) & {"T24", "T25", "T26"} for x in b.bound)


def test_age_class_with_age_condition_is_that_age(monkeypatch) -> None:
    # '스무 살 안 됐는데 혼자 사는 사람' + 연령별: 20세 미만 하나
    # (연령 칸 모두의 비교가 아님, V2-144)
    items = Dim(0, "항목", {"T00": "1인가구"})
    age = Dim(2, "연령별", {"000": "합계", "024": "20세미만", "025": "20~24세", "030": "25~29세"})
    _table(monkeypatch, [items, age])
    slots = _slots(items=["20세 미만"], classes=["연령"])
    b = binding.bind("T", "스무 살 안 됐는데 혼자 사는 사람", {}, slots)
    plan = binding.retry_plan(b, "스무 살 안 됐는데 혼자 사는 사람", {})
    assert plan is not None and plan.multi is None and plan.parsed["ages"] == [[0, 19]]


def test_retry_plan_groups(monkeypatch) -> None:
    items = Dim(0, "항목", {"T10": "가구_계", "T22": "3자녀", "T23": "4자녀", "T24": "5자녀 이상"})
    _table(monkeypatch, [items])
    b = binding.bind("T", "자녀 셋 이상 가구", {}, _slots(items=["셋 이상"]))
    plan = binding.retry_plan(b, "자녀 셋 이상 가구", {})
    assert plan.multi == (0, ["T22", "T23", "T24"], "sum") and plan.forced == {}
    # 국적별 순위: 기타 · 미상은 뺀다
    nation = Dim(1, "국적별", {"0": "계", "1": "미국", "2": "중국", "9": "기타 및 미상"})
    _table(monkeypatch, [Dim(0, "항목", {"T3": "혼인"}), nation])
    b = binding.bind("T", "국적별 혼인 상위 3", {"type": "rank"}, _slots(classes=["국적"]))
    plan = binding.retry_plan(b, "국적별 혼인 상위 3", {"type": "rank"})
    assert plan.multi == (1, ["1", "2"], "rank") and plan.forced == {0: "T3"}
    # 표 전체가 여성: 질문 해석의 성별을 뺀다
    married = Dim(0, "항목", {"T05": "출생자녀 3명", "T06": "출생자녀 4명", "T07": "출생자녀 5명"})
    _table(monkeypatch, [married], name="출생자녀수별 기혼여성인구")
    b = binding.bind("T", "셋 이상 낳은 기혼 여성", {"sex": "여"}, _slots(items=["셋 이상"]))
    assert binding.retry_plan(b, "셋 이상 낳은 기혼 여성", {"sex": "여"}).parsed["sex"] is None
    # 못 이었으면 다시 계산하지 않는다
    b = binding.bind("T", "부부 불화", {}, _slots(items=["부부 불화"]))
    assert binding.retry_plan(b, "부부 불화", {}) is None


def test_retry_plan_marks_default_cells_quiet(monkeypatch) -> None:
    """말하지 않아 기본으로 고른 칸(합계)은 고르기만 하고 지표 이름에 쓰지 않는다
    ("사망자수"가 "계 수"로, D-125 11/24)"""
    items = Dim(0, "항목", {"T1": "사망자수"})
    sex = Dim(1, "성별", {"0": "계", "1": "남자", "2": "여자"})
    _table(monkeypatch, [items, sex])
    b = binding.bind("T", "작년에 돌아가신 분", {}, _slots(), {0: "T1"})
    plan = binding.retry_plan(b, "작년에 돌아가신 분", {})
    assert plan.forced == {0: "T1", 1: "0"} and plan.quiet == {1: "total"}
    got, _ = d.choose("작년에 돌아가신 분", {}, [items, sex], None, plan.forced, quiet=plan.quiet)
    assert got.pick == {0: "T1", 1: "0"} and 1 not in got.matched


def test_compare_of_decimal_values_is_rounded() -> None:
    assert d._decimals(51.93) == 2 and d._decimals(1438) == 0 and d._decimals(47.4) == 1


def test_forced_cell_settles_age_cells(monkeypatch) -> None:
    # 그 연령이 든 칸이 여럿이면 규칙은 못 고르지만, 칸 고르기 LLM이 하나를 고르면 그 칸 (V2-013)
    names = ["일반가구_계", "가구원 모두 65세이상", "가구원 65세이상+65세미만"]
    names.append("가구원 65세이상 없음")
    items = Dim(0, "항목", {str(i): n for i, n in enumerate(names)})
    _table(monkeypatch, [items], name="고령자(65세이상) 가구(일반가구)")
    q = "식구가 전부 65살 이상인 집"
    b = binding.bind("T", q, {"ages": [[65, None]]}, _slots(), forced={0: "1"})
    assert b.status == binding.BOUND
    plan = binding.retry_plan(b, q, {"ages": [[65, None]]})
    assert plan.forced == {0: "1"} and plan.parsed["ages"] is None


def test_scenario_default_over_llm_pick(monkeypatch) -> None:
    # 시나리오를 말하지 않았는데 LLM이 '현수준'을 골랐으면 기본값 '중위' (V2-050).
    # 질문이 그 시나리오를 말했으면 LLM의 칸 그대로
    items = Dim(0, "항목", {"T10": "총인구"})
    scen = Dim(1, "시나리오별", {"01": "중위 추계", "02": "고위 추계", "03": "저위 추계",
                                 "04": "출산율 현수준 추계"})  # fmt: skip
    _table(monkeypatch, [items, scen])
    b = binding.bind("T", "2072년 인구", {}, _slots(), forced={0: "T10", 1: "04"})
    plan = binding.retry_plan(b, "2072년 인구", {})
    assert plan.forced == {0: "T10", 1: "01"}
    q = "출산율 현수준 추계로 2072년 인구"
    b = binding.bind("T", q, {}, _slots(), forced={0: "T10", 1: "04"})
    assert binding.retry_plan(b, q, {}).forced == {0: "T10", 1: "04"}


def test_special_projection_table_without_baseline(monkeypatch) -> None:
    # 중위 추계가 없는 특별 추계 표: 시나리오를 말하지 않았으면 잇지 못함 → 다음 후보 표로 (V2-050)
    items = Dim(0, "항목", {"T10": "총인구"})
    scen = Dim(1, "시나리오별", {"01": "높은 출산율 추계(출산율-고위)",
                                 "03": "출산율 현수준 추계",
                                 "10": "빠른 고령화 추계(출산율-저위)"})  # fmt: skip
    _table(monkeypatch, [items, scen])
    b = binding.bind("T", "2072년 인구", {}, _slots(), forced={0: "T10", 1: "03"})
    assert b.status == binding.PARTIAL and any("중위" in u.why for u in b.unbound)
    q = "빠른 고령화 추계로 2072년 인구"
    b = binding.bind("T", q, {}, _slots(), forced={0: "T10", 1: "10"})
    assert b.status == binding.BOUND


def test_no_retry_for_ratio_questions(monkeypatch) -> None:
    # 비율 질문은 잇기의 칸으로 다시 계산하지 않는다 (V2-090: 분자 = 분모 → 100%)
    items = Dim(0, "항목", {"0": "일반가구_계", "1": "가구원 모두 65세이상"})
    _table(monkeypatch, [items])
    q = "전체 가구 중에 식구가 전부 65살 이상인 집은 몇 퍼센트야?"
    b = binding.bind("T", q, {"ages": [[65, None]]}, _slots(), forced={0: "1"})
    assert b.status == binding.BOUND and binding.retry_plan(b, q, {"ages": [[65, None]]}) is None
