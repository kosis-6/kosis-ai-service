import pytest

from app.prompts import recell as prompt
from app.services import binding, recell
from app.services.binding import Binding, Bound, Unbound
from app.services.dynamic_indicator import Dim

HOUSEHOLD = [
    Dim(0, "항목", {"T1": "일반가구", "T2": "가구원 모두 65세이상", "T3": "가구원 65세이상 없음"}),
    Dim(1, "행정구역별", {"00": "전국", "38": "경상남도"}),
]
FOREIGNER = [
    Dim(0, "항목", {"T1": "인원", "T2": "구성비"}),
    Dim(1, "국가별", {"A001": "계", "A002": "태국", "A003": "중국"}),
]


@pytest.fixture(autouse=True)
def schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dynamic_indicator as di

    monkeypatch.setattr(di, "schema", lambda tbl_id: (HOUSEHOLD, 1, ""))


def _household() -> Binding:
    return Binding(
        "DT_X",
        bound=[Bound("region", "경상남도", "region", 1)],
        unbound=[
            Unbound("age", "65세 이상", "그 연령이 든 칸이 여럿"),
            Unbound("항목", "", "합계 칸이 없음 (되묻기)"),
            Unbound("topic", "고령자 가구 수", "질문의 말과 맞는 칸이 없음"),
        ],
    )


def _pick(no, how, dim=None, codes=(), broader=False):
    return {"no": no, "how": how, "dim": dim, "codes": list(codes), "broader": broader, "why": ""}


def test_cell_pick_binds_and_clears_the_dim_and_topic() -> None:
    b = _household()
    conds = recell.conditions(b, HOUSEHOLD, None)
    assert [u.cond for u in conds] == ["age", "topic"]
    raw = {"picks": [_pick(1, prompt.CELLS, 0, ["T2"]), _pick(2, prompt.NONE)]}
    fix = recell.apply(b, conds, raw, HOUSEHOLD, 1)
    assert fix is not None and fix.binding.status == binding.BOUND
    assert fix.binding.picks() == {0: ["T2"]}
    assert "가구원 모두 65세이상" in fix.notice
    plan = binding.retry_plan(fix.binding, "고령자 가구 수", {"ages": [[65, None]]})
    assert plan.forced == {0: "T2"} and plan.parsed["ages"] is None


def test_class_pick_replaces_total_and_ranks() -> None:
    b = Binding(
        "DT_Y",
        bound=[Bound("국가별", "계", "total", 1, ["A001"], ["계"])],
        unbound=[
            Unbound("class", "국적", "그 분류가 없음"),
            Unbound("항목", "", "합계 칸이 없음 (되묻기)"),
            Unbound("topic", "불법체류 외국인 수", "질문의 말과 맞는 칸이 없음"),
        ],
    )
    conds = recell.conditions(b, FOREIGNER, None)
    raw = {"picks": [_pick(1, prompt.CLASS, 1), _pick(2, prompt.CELLS, 0, ["T1"])]}
    fix = recell.apply(b, conds, raw, FOREIGNER, None)
    assert fix is not None
    assert not any(x.by == "total" for x in fix.binding.bound)
    assert {(x.by, x.dim) for x in fix.binding.bound} == {("dim", 1), ("value", 0)}


def test_broader_cell_is_allowed_with_a_notice() -> None:
    b = Binding("DT_Z", unbound=[Unbound("item", "농업", "맞는 칸 없음")])
    conds = recell.conditions(b, FOREIGNER, None)
    raw = {"picks": [_pick(1, prompt.TABLE, broader=True)]}
    fix = recell.apply(b, conds, raw, FOREIGNER, None, "농림어업종사자 혼인")
    assert fix is not None and fix.binding.status == binding.BOUND
    assert fix.notice == (
        "질문의 '농업'은(는) 표에 따로 없어 '농림어업종사자 혼인' 기준으로 답했습니다"
    )


def test_none_unknown_code_or_clash_keeps_refusing() -> None:
    b = _household()
    conds = recell.conditions(b, HOUSEHOLD, None)
    assert recell.apply(b, conds, {"picks": [_pick(1, prompt.NONE)]}, HOUSEHOLD, 1) is None
    bad = {"picks": [_pick(1, prompt.CELLS, 0, ["T9"])]}
    assert recell.apply(b, conds, bad, HOUSEHOLD, 1) is None
    region = {"picks": [_pick(1, prompt.CELLS, 1, ["38"])]}
    assert recell.apply(b, conds, region, HOUSEHOLD, 1) is None
    clash = Binding(
        "DT_X",
        bound=[Bound("word", "일반가구", "value", 0, ["T1"], ["일반가구"])],
        unbound=[Unbound("age", "65세 이상", "그 연령이 든 칸이 여럿")],
    )
    conds = recell.conditions(clash, HOUSEHOLD, None)
    raw = {"picks": [_pick(1, prompt.CELLS, 0, ["T2"])]}
    assert recell.apply(clash, conds, raw, HOUSEHOLD, 1) is None


def test_only_blank_dim_left_asks_with_the_topic() -> None:
    b = Binding("DT_X", unbound=[Unbound("항목", "", "합계 칸이 없음 (되묻기)")])
    conds = recell.conditions(b, HOUSEHOLD, "평균나이")
    assert [(u.cond, u.value) for u in conds] == [("topic", "평균나이")]
    assert recell.conditions(b, HOUSEHOLD, None) == []


def test_ratio_plan_only_without_multi() -> None:
    b = Binding("DT_X", bound=[Bound("age", "65세 이상", "value", 0, ["T2"], ["x"])])
    assert binding.retry_plan(b, "고령 가구 비율", {"type": "ratio"}) is None
    plan = binding.retry_plan(b, "고령 가구 비율", {"type": "ratio"}, ratio_ok=True)
    assert plan is not None and plan.forced == {0: "T2"}
    many = Binding("DT_X", bound=[Bound("item", "a", "value", 0, ["T1", "T2"], ["a", "b"])])
    assert binding.retry_plan(many, "비율", {"type": "ratio"}, ratio_ok=True) is None


def test_second_item_on_same_dim_joins_for_comparison() -> None:
    b = Binding(
        "DT_X",
        bound=[Bound("item", "지하철", "value", 0, ["T1"], ["일반가구"])],
        unbound=[Unbound("item", "시내버스", "맞는 칸 없음")],
    )
    conds = recell.conditions(b, HOUSEHOLD, None)
    raw = {"picks": [_pick(1, prompt.CELLS, 0, ["T2"], broader=True)]}
    fix = recell.apply(b, conds, raw, HOUSEHOLD, 1)
    assert fix is not None and fix.binding.picks() == {0: ["T1", "T2"]}
    assert "기준으로 답했습니다" in fix.notice
    plan = binding.retry_plan(fix.binding, "시내버스랑 지하철 차이", {})
    assert plan.multi is not None and plan.multi[1] == ["T1", "T2"]


def test_broader_cell_clashing_with_chosen_cell_reads_as_table_scope() -> None:
    b = Binding(
        "DT_X",
        bound=[Bound("word", "외국인 아내", "value", 0, ["T3"], ["가구원 65세이상 없음"])],
        unbound=[Unbound("item", "농업", "맞는 칸 없음")],
    )
    conds = recell.conditions(b, HOUSEHOLD, None)
    raw = {"picks": [_pick(1, prompt.CELLS, 0, ["T1"], broader=True)]}
    fix = recell.apply(b, conds, raw, HOUSEHOLD, 1)
    assert fix is not None and fix.binding.picks() == {0: ["T3"]}
    assert fix.notice == "질문의 '농업'은(는) 표에 따로 없어 '일반가구' 기준으로 답했습니다"
    exact = {"picks": [_pick(1, prompt.CELLS, 0, ["T1"])]}
    assert recell.apply(b, conds, exact, HOUSEHOLD, 1) is None


def test_class_pick_with_rank_words_ranks() -> None:
    b = Binding("DT_X", bound=[Bound("class", "국적", "dim", 0, note="항목")])
    plan = binding.retry_plan(b, "불법체류 외국인 국적 1, 2, 3위 알려줘", {"type": "value"})
    assert plan.multi[2] == "rank"
    plan = binding.retry_plan(b, "국적별로 보여줘", {"type": "value"})
    assert plan.multi[2] == "compare"


def test_class_split_made_by_binding_is_not_a_single_value_refusal() -> None:
    from app.services import dynamic_indicator as di

    slots = {"conditions": {"classes": ["국적"]}, "cells_by_binding": True}
    plan = di.Plan(pick={0: "T1"}, multi=(1, ["A002", "A003"], "rank"))
    assert di.uncovered(slots, plan, FOREIGNER, {"type": "value"}, None, "국적 1, 2, 3위") is None
    plain = {"conditions": {"classes": ["국적"]}}
    why = di.uncovered(plain, plan, FOREIGNER, {"type": "value"}, None, "국적 1, 2, 3위")
    assert why == "나눠 보기(국적별)는 값 하나로 답할 수 없음"
