"""통합 보고서 생성: 비교 검사 R1~R6 · LLM 출력 검증 · 자리표시자 채우기 · 대체 문장 (명세서 5장)"""

import asyncio

import pytest

from app.core.config import get_settings
from app.services import report_compare as rc
from app.services import report_writer as w


def fig(
    label,
    value,
    unit="가구",
    *,
    ind="1인가구",
    region="서울특별시",
    period="2020년",
    population=None,
    tbl="DT_A",
    kind="value",
    proj=False,
    rows=None,
    sex=None,
):
    return {
        "label": label,
        "indicator": ind,
        "value": value,
        "unit": unit,
        "kind": kind,
        "conditions": {
            "region": region,
            "period": period,
            "sex": sex,
            "ages": None,
            "population": population,
        },
        "is_projection": proj,
        "formula": None,
        "operands": [],
        "rows": rows or [],
        "change": None,
        "tbl_ids": [tbl],
    }


def snap(f, headline=None, turn_id=1):
    return {
        "turn_id": turn_id,
        "snapshot": {
            "figure": f,
            "question": "q",
            "summary": {"headline": headline} if headline else None,
            "sources": [{"name": "s"}],
        },
    }


def items(*figs):
    return rc.items_of([snap(f, turn_id=i + 1) for i, f in enumerate(figs)])


# ---- 비교 검사 ------------------------------------------------------------------------


def test_items_numbered_in_order_and_skip_without_figure():
    its = rc.items_of([snap(fig("a", 1)), {"turn_id": 9, "snapshot": {}}, snap(fig("b", 2))])
    assert [i["id"] for i in its] == ["r1", "r2"]


@pytest.mark.parametrize(
    ("a", "b", "rule"),
    [
        (fig("a", 1, population="1인가구"), fig("b", 2, population="1인가구(20세 이상)"), "R1"),
        (fig("a", 1), fig("b", 2, unit="명"), "R2"),
        (fig("a", 1, tbl="DT_A"), fig("b", 2, tbl="DT_B", region="부산"), "R5"),
        (
            fig("a", 1, ind="출생아 수", unit="명"),
            fig("b", 2, ind="사망자 수", unit="명", period="2021년"),
            "R4",
        ),
        (fig("a", 1), fig("b", 2, region="부산광역시", period="2021년"), "R4"),
    ],
)
def test_pair_rules(a, b, rule):
    x, y = items(a, b)
    assert rc.check_pair(x, y)[0] == rule


def test_same_series_over_time_is_change():
    out = rc.compare(items(fig("a", 100), fig("b", 120, period="2025년")))
    assert out["not_comparable"] == []
    (d,) = out["derived"]
    assert d["kind"] == "change" and d["value"] == 20 and d["rate"] == 20.0
    assert d["direction"] == "증가" and d["of"] == ["r1", "r2"]


@pytest.mark.parametrize(
    ("earlier", "later"),
    [("2023년", "2024년"), ("2024년 9월", "2024년 10월")],
)
def test_change_uses_chronological_order(earlier, later):
    first, second = fig("이전", 12866, period=earlier), fig("이후", 13063, period=later)
    (change,) = rc.compare(items(second, first))["derived"]
    assert change["of"] == ["r2", "r1"]
    assert change["direction"] == "증가" and change["value"] == 197 and change["rate"] == 1.5
    assert change["meaning"].startswith(f"{earlier} → {later}")


def test_percent_change_is_percent_point_without_rate():
    out = rc.compare(items(fig("a", 10.0, "%"), fig("b", 12.5, "%", period="2025년")))
    (d,) = out["derived"]
    assert d["unit"] == "%p" and "rate" not in d


def test_region_gap():
    out = rc.compare(items(fig("a", 100), fig("b", 70, region="부산광역시")))
    (d,) = out["derived"]
    assert d["kind"] == "gap" and d["value"] == 30 and d["direction"] == "서울특별시가 더 큼"
    assert (d["larger"], d["smaller"]) == ("r1", "r2")


def test_different_indicators_same_place_no_derived():
    out = rc.compare(items(fig("a", 100), fig("b", 60, ind="여성 1인가구")))
    assert out["derived"] == [] and out["not_comparable"] == []


def test_projection_mixed_and_limits():
    out = rc.compare(items(fig("a", 100), fig("b", 150, period="2040년", proj=True)))
    assert out["projection_mixed"] == [["r1", "r2"]]
    assert any("장래추계" in x for x in out["limits"])


def test_trend_needs_three_periods_or_series():
    two = rc.compare(items(fig("a", 1), fig("b", 2, period="2025년")))
    three = rc.compare(
        items(fig("a", 1), fig("b", 2, period="2015년"), fig("c", 3, period="2010년"))
    )
    series = rc.compare(items(fig("a", 3, kind="series", rows=[{}, {}, {}])))
    assert (two["trend_ok"], three["trend_ok"], series["trend_ok"]) == (False, True, True)
    assert any("추세" in x for x in two["limits"])


def test_value_text_formats():
    (it,) = items(fig("a", 1390701))
    assert rc.value_text(it) == "1,390,701가구"
    assert rc.value_text({"value": 20, "unit": "가구", "rate": 20.0}) == "20가구(+20.0%)"


# ---- LLM 출력 검증 --------------------------------------------------------------------


def setup_two():
    its = items(fig("2020년 서울 1인가구", 100), fig("2025년 서울 1인가구", 120, period="2025년"))
    cmp = rc.compare(its)
    return its, cmp, w._allowed_numbers("제목", None, its, cmp)


def only_sentences(problems):
    """문장 하나씩 보는 시험: '값을 한 번씩은 쓰라'는 보고서 전체 규칙은 빼고 본다"""
    return [p for p in problems if not p.startswith("값이 있는 결과는")]


def s(text, refs=("r1",)):
    return {"text": text, "refs": list(refs)}


def test_check_keeps_good_sentences_and_adds_placeholder_refs():
    its, cmp, allowed = setup_two()
    out = {
        "summary": [s("{c1.statement}", [])],
        "analysis": [],
    }
    kept, problems, dropped = w.check(out, its, cmp, allowed)
    assert only_sentences(problems) == [] and dropped == []
    assert kept["summary"][0]["refs"] == ["c1", "r1", "r2"]


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("서울 1인가구는 약 140만입니다.", "숫자를 직접"),
        ("값은 {r9.value}입니다.", "없는 자리표시자"),
        ("고령화 때문에 {r1.value}입니다.", "원인"),
        ("{r1.value}로 증가세입니다.", "비교 · 증감"),
        ("고령화에 대비가 필요합니다.", "추측"),
        ("JSON", "JSON"),
    ],
)
def test_check_problems_cause_retry(text, why):
    its, cmp, allowed = setup_two()
    out = None if text == "JSON" else {"summary": [s(text)], "analysis": []}
    _, problems, _ = w.check(out, its, cmp, allowed)
    assert any(why in p for p in problems)


def test_years_in_labels_are_allowed():
    its, cmp, allowed = setup_two()
    kept, problems, _ = w.check(
        {"summary": [s("2020년 서울 1인가구는 {r1.value}였습니다.")]}, its, cmp, allowed
    )
    assert only_sentences(problems) == [] and kept["summary"]


def test_not_comparable_pair_sentence_dropped():
    its = items(fig("a", 1, population="전체"), fig("b", 2, population="20세 이상"))
    cmp = rc.compare(its)
    out = {"summary": [s("{r1.value}입니다.", ["r1"]), s("{r1.value}와 {r2.value}입니다.", [])]}
    kept, problems, dropped = w.check(out, its, cmp, w._allowed_numbers("t", None, its, cmp))
    assert only_sentences(problems) == [] and len(kept["summary"]) == 1
    assert dropped[0]["why"].startswith("비교할 수 없는")


def test_projection_sentence_needs_word():
    its = items(fig("a", 1, proj=True, period="2040년"))
    cmp = rc.compare(its)
    allowed = w._allowed_numbers("t", None, its, cmp)
    _, bad, _ = w.check({"summary": [s("{r1.value}입니다.")]}, its, cmp, allowed)
    _, good, _ = w.check({"summary": [s("{r1.value}로 예상됩니다.")]}, its, cmp, allowed)
    assert bad and not good


def test_null_value_item_cannot_use_placeholder():
    its = items(fig("비교", None, kind="compare"))
    cmp = rc.compare(its)
    _, problems, _ = w.check({"summary": [s("{r1.value}")]}, its, cmp, set())
    assert problems


# ---- write: LLM 흉내 -----------------------------------------------------------------


def run_write(monkeypatch, outputs, figs=None, title="서울 1인 가구"):
    calls = []

    async def ask(messages):
        calls.append(messages)
        out = outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(w, "_ask", ask)
    figs = figs or [
        fig("2020년 서울특별시 1인가구", 1390701),
        fig("2025년 서울특별시 1인가구", 1500000, period="2025년"),
    ]
    snaps = [
        snap(f, headline=f"{f['label']}는 {f['value']:,}가구입니다.", turn_id=i + 1)
        for i, f in enumerate(figs)
    ]
    content = asyncio.run(w.write(title, "규모", snaps))
    return content, calls


def test_write_fills_values(monkeypatch):
    good = {
        "summary": [s("{c1.statement}", ["c1"])],
        "analysis": [s("2020년 서울특별시 1인가구는 {r1.value}였습니다.")],
    }
    content, calls = run_write(monkeypatch, [good])
    assert len(calls) == 1
    assert content["generated_by"] == "hcx"
    assert (
        content["summary"][0]["text"]
        == "서울특별시 1인가구는 2020년(1,390,701가구)에서 2025년(1,500,000가구)으로 "
        "증가했습니다 (증감 109,299가구(+7.9%))."
    )
    assert content["items"][0]["value_text"] == "1,390,701가구"
    assert content["derived"][0]["value_text"] == "109,299가구(+7.9%)"
    llm_text = calls[0][1]["content"]
    assert "1390701" not in llm_text and "1,390,701" not in llm_text  # LLM에 값을 주지 않는다


def test_write_retries_once_with_problems(monkeypatch):
    bad = {"summary": [s("약 140만 가구입니다.")], "analysis": []}
    good = {"summary": [s("{r1.value}입니다.")], "analysis": []}
    content, calls = run_write(monkeypatch, [bad, good])
    assert len(calls) == 2 and "숫자를 직접" in calls[1][-1]["content"]
    assert content["generated_by"] == "hcx" and content["warnings"] == []


def test_write_replaces_the_reproduced_reversed_sentence(monkeypatch):
    figs = [
        fig("2024년 부산 출생아 수", 13063, "명", ind="출생아 수", region="부산", period="2024년"),
        fig("2023년 부산 출생아 수", 12866, "명", ind="출생아 수", region="부산", period="2023년"),
    ]
    bad = s("2024년 {r1.value}는 2023년 {r2.value}에 비해 감소했습니다.", ["c1"])
    content, calls = run_write(monkeypatch, [{"summary": [bad]}], figs)
    assert len(calls) == 1
    assert content["generated_by"] == "hcx" and content["warnings"] == []
    assert content["summary"][0]["text"] == (
        "부산 출생아 수는 2023년(12,866명)에서 2024년(13,063명)으로 "
        "증가했습니다 (증감 197명(+1.5%))."
    )
    assert content["summary"][0]["refs"] == ["c1", "r1", "r2"]


def test_write_falls_back_after_two_failures(monkeypatch):
    bad = {"summary": [s("약 140만 가구입니다.")], "analysis": []}
    content, calls = run_write(monkeypatch, [bad, dict(bad)])
    assert len(calls) == 2 and content["generated_by"] == "fallback"
    assert content["summary"][0]["text"] == "2020년 서울특별시 1인가구는 1,390,701가구입니다."
    assert content["warnings"]


def test_write_falls_back_when_llm_down(monkeypatch):
    content, calls = run_write(monkeypatch, [RuntimeError("hcx down")])
    assert len(calls) == 1 and content["generated_by"] == "fallback"
    assert len(content["summary"]) == 2
    assert content["analysis"][0]["refs"] == ["c1", "r1", "r2"]
    assert "증감 109,299가구(+7.9%)" in content["analysis"][0]["text"]


def test_write_without_items_raises(monkeypatch):
    with pytest.raises(ValueError):
        asyncio.run(w.write("t", None, [{"turn_id": 1, "snapshot": {}}]))


def test_gemma_setting_picks_gemma_client(monkeypatch):
    used = []

    class Fake:
        def __init__(self, *a, **k):
            used.append(type(self).__name__)

        async def chat(self, *a, **k):
            from types import SimpleNamespace

            return SimpleNamespace(content='{"summary": [], "analysis": []}', usage={})

    monkeypatch.setattr(w.search_llm, "GemmaClient", type("GemmaClient", (Fake,), {}))
    monkeypatch.setattr(w, "HcxClient", type("HcxClient", (Fake,), {}))
    monkeypatch.setenv("REPORT_LLM", "gemma")
    get_settings.cache_clear()
    try:
        asyncio.run(w._ask([{"role": "user", "content": "x"}]))
    finally:
        get_settings.cache_clear()
    assert used == ["GemmaClient"]


# ---- 실제 HCX 시험에서 나온 문제 ------------------------------------------------------


def test_fill_drops_unit_written_again():
    text = w.fill(
        "차이는 {c1.value} 가구입니다. {r1.value}가구",
        {"c1": "9가구", "r1": "1가구"},
        {"c1": "가구", "r1": "가구"},
    )
    assert text == "차이는 9가구입니다. 1가구"


def rows(*vals):
    return [{"label": f"{2015 + i}년", "value": v} for i, v in enumerate(vals)]


@pytest.mark.parametrize(
    ("vals", "direction", "steady"),
    [
        ((10, 12, 15), "증가", True),
        ((10, 9, 15), "증가", False),
        ((5, 4, 3), "감소", True),
    ],
)
def test_series_change(vals, direction, steady):
    out = rc.compare(items(fig("추이", vals[-1], "%", kind="series", rows=rows(*vals))))
    (d,) = out["derived"]
    assert (d["kind"], d["direction"], d["steady"], d["unit"]) == (
        "series_change",
        direction,
        steady,
        "%p",
    )


def test_steady_words_need_steady_derived():
    its = items(fig("추이", 15, "%", kind="series", rows=rows(10, 9, 15)))
    cmp = rc.compare(its)
    allowed = w._allowed_numbers("t", None, its, cmp)
    _, bad, _ = w.check(
        {"summary": [s("꾸준히 늘어 {r1.value}입니다.", ["r1", "c1"])]}, its, cmp, allowed
    )
    assert bad
    its = items(fig("추이", 15, "%", kind="series", rows=rows(10, 12, 15)))
    cmp = rc.compare(its)
    _, good, _ = w.check(
        {"summary": [s("꾸준히 늘어 {r1.value}입니다.", ["r1", "c1"])]}, its, cmp, allowed
    )
    assert not good


def test_limits_name_results_not_ids():
    out = rc.compare(
        items(
            fig("서울 1인가구", 1, population="전체"),
            fig("서울 1인가구(20세 이상)", 2, population="20세 이상"),
            fig("서울 고령인구 비율", 3, "%"),
        )
    )
    assert out["limits"][0].startswith(
        "'서울 1인가구'와 '서울 1인가구(20세 이상)'는 직접 비교하지 않았습니다: 모집단이 다릅니다"
    )
    assert not any("고령인구" in x for x in out["limits"])  # 단위가 다른 쌍은 적지 않는다
    assert "r1" not in " ".join(out["limits"])


def test_trend_word_blocked_without_three_periods():
    its = items(fig("추이", 15, "%", kind="series", rows=rows(10, 15)))
    cmp = rc.compare(its)
    _, problems, _ = w.check(
        {"summary": [s("{c1.value} 늘어 증가세입니다.", ["c1"])]},
        its,
        cmp,
        w._allowed_numbers("t", None, its, cmp),
    )
    assert any("추세" in p for p in problems)


def test_comparison_needs_derived_ref():
    its, cmp, allowed = setup_two()
    _, bad, _ = w.check({"summary": [s("{r2.value}로 늘었습니다.", ["r2"])]}, its, cmp, allowed)
    _, good, _ = w.check({"summary": [s("{c1.statement}", ["c1"])]}, its, cmp, allowed)
    assert only_sentences(bad) and not only_sentences(good)


def test_count_suffix_same_indicator():
    a, b = items(fig("a", 1, ind="1인 가구 수"), fig("b", 2, ind="1인가구", period="2025년"))
    assert rc.check_pair(a, b) == (None, None)


def test_second_try_keeps_good_sentences(monkeypatch):
    mixed = {"summary": [s("{r1.value}입니다."), s("약 140만 가구입니다.")], "analysis": []}
    content, calls = run_write(monkeypatch, [mixed, dict(mixed)])
    assert len(calls) == 2 and content["generated_by"] == "hcx"
    assert [x["text"] for x in content["summary"]] == ["1,390,701가구입니다."]
    assert content["dropped"][0]["text"] == "약 140만 가구입니다."
    assert content["warnings"] == ["검증을 통과하지 못한 문장 1개는 뺐습니다."]


def test_fallback_marks_projection():
    its = items(fig("2040년 서울 1인가구", 1842002, period="2040년", proj=True))
    assert w._fallback(its)["summary"][0]["text"].endswith("(장래추계 예상값)")


def test_series_change_uses_answer_change():
    f = fig("추이", 20.43, "%", kind="series", rows=rows(12.6, 15.0, 20.43))
    f["change"] = {"diff": 7.84}
    (d,) = rc.compare(items(f))["derived"]
    assert d["value"] == 7.84


def test_every_value_must_appear_once():
    its, cmp, allowed = setup_two()
    _, problems, _ = w.check({"summary": [s("{r1.value}입니다.")]}, its, cmp, allowed)
    assert problems == ["값이 있는 결과는 {rN.value}로 한 번씩은 쓰세요. 빠진 것: r2"]


def test_missing_values_filled_from_summaries_on_second_try(monkeypatch):
    empty = {"summary": [s("서울 1인가구 결과를 담았습니다.")], "analysis": []}
    content, calls = run_write(monkeypatch, [empty, dict(empty)])
    assert len(calls) == 2 and content["generated_by"] == "hcx"
    assert [x["text"] for x in content["analysis"][:2]] == [
        "2020년 서울특별시 1인가구는 1,390,701가구입니다.",
        "2025년 서울특별시 1인가구는 1,500,000가구입니다.",
    ]


def test_actual_values_not_written_as_forecast():
    its, cmp, allowed = setup_two()
    out = {"summary": [s("{r2.value}가 될 것으로 보입니다.", ["r2"])]}
    _, problems, _ = w.check(out, its, cmp, allowed)
    assert any("예상 · 전망처럼" in p for p in problems)


# ---- #246 리뷰 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "refs"),
    [
        ("{r2.value}는 {r1.value}보다 {c1.direction}했습니다.", ["c1"]),
        ("{r1.value}보다 {r2.value}가 감소했습니다.", []),
        ("변화는 {c1.statement}입니다.", []),
    ],
)
def test_time_comparison_is_replaced_with_a_complete_statement(text, refs):
    its, cmp, allowed = setup_two()
    out = {"summary": [s(text, refs)], "analysis": [s("{c1.statement}", [])]}
    kept, problems, dropped = w.check(out, its, cmp, allowed)
    assert problems == [] and dropped == []
    assert kept["summary"][0]["text"].endswith("증가했습니다 (증감 {c1.value}).")
    assert kept["summary"][0]["refs"] == ["c1", "r1", "r2"]
    assert kept["analysis"] == []


def test_unknown_time_comparison_is_dropped():
    its, cmp, allowed = setup_two()
    kept, problems, dropped = w.check({"summary": [s("{c9.statement}")]}, its, cmp, allowed)
    assert not kept["summary"] and dropped and any("{cN.statement}" in p for p in problems)


def test_series_opposite_direction_is_caught():
    its = items(fig("추이", 15, "%", kind="series", rows=rows(10, 12, 15)))
    cmp = rc.compare(its)
    out = {"summary": [s("{r1.value}로 하락했습니다.", ["r1", "c1"])]}
    _, problems, _ = w.check(out, its, cmp, w._allowed_numbers("t", None, its, cmp))
    assert any("방향이 계산과 반대" in p for p in problems)


def test_different_ages_and_regions_not_a_gap():
    """서울 20대와 부산 30대를 지역 차이로 계산하지 않는다"""
    a = fig("서울 20대 1인가구", 100)
    a["conditions"]["ages"] = "20~29세"
    b = fig("부산 30대 1인가구", 70, region="부산광역시")
    b["conditions"]["ages"] = "30~39세"
    out = rc.compare(items(a, b))
    assert out["derived"] == []
    assert out["not_comparable"][0]["rule"] == "R1"
    assert "연령 조건" in out["not_comparable"][0]["why"]


def test_two_regions_and_sexes_differ_not_a_gap():
    out = rc.compare(items(fig("a", 100, sex="여"), fig("b", 70, region="부산광역시", sex="남")))
    assert out["derived"] == []


def test_invalid_json_twice_falls_back(monkeypatch):
    """모델이 두 번 연속 JSON 을 주지 않아도 500 이 아니라 대체 요약"""
    content, calls = run_write(monkeypatch, [None, "not json"])
    assert len(calls) == 2 and content["generated_by"] == "fallback"
    assert content["summary"]
