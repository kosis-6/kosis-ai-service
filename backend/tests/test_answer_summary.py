"""답변 요약(answer_summary): 계산 결과 → 핵심 한 문장 + 덧붙일 사실 + 주의

DB 없이 보려고 계산 결과를 직접 만든다 (indicators.py·lookup.py가 돌려주는 모양)."""

import re

import pytest

from app.services import answer_summary as a
from app.services import qa

SRC = {"source": "테스트 출처", "basis": "테스트 기준", "tbl_id": "DT_TEST", "url": None}


def trend(region: str, values: list[float], start: int = 2020, **more) -> dict:
    """연도별 값으로 추이 결과를 만든다 (전년 대비·최고·최저 해는 qa가 붙이는 그대로)"""
    series = [{"year": start + i, "value": v} for i, v in enumerate(values)]
    diff = round(values[-1] - values[0], 3)  # 실제 계산 결과도 반올림한 값이다
    r = {
        "status": "ok",
        "indicator": "출생아 수",
        "kind": "count",
        "unit": "명",
        "region": region,
        "sex": "계",
        "series": series,
        "change": {
            "from_year": start,
            "to_year": start + len(values) - 1,
            "diff": diff,
            "diff_unit": "명",
            "rate": round(diff / values[0] * 100, 2),
            "rate_unit": "%",
        },
        "notes": [],
        **SRC,
        **more,
    }
    qa._add_yearly_change(r)
    qa._add_trend_facts(r)
    return r


VALUE = {
    "status": "ok",
    "indicator": "고령인구 비율",
    "kind": "ratio",
    "unit": "%",
    "region": "부산광역시",
    "year": 2025,
    "sex": "계",
    "value": 24.5,
    "notes": [],
    **SRC,
}
RANK = {
    "status": "ok",
    "indicator": "고령인구 비율",
    "kind": "ratio",
    "unit": "%",
    "year": 2025,
    "sex": "계",
    "level": "시도",
    "order": "높은 순",
    "total_regions": 4,
    "ranking": [
        {"region": "전라남도", "value": 27.2, "rank": 1},
        {"region": "경상북도", "value": 26.04, "rank": 2},
        {"region": "부산광역시", "value": 24.5, "rank": 3},
        {"region": "세종특별자치시", "value": 11.6, "rank": 4},
    ],
    "notes": [],
    **SRC,
}
COMPARE = {
    "status": "ok",
    "indicator": "고령인구 비율",
    "kind": "ratio",
    "unit": "%",
    "year": 2025,
    "sex": "계",
    "values": [
        {"region": "부산광역시", "value": 24.5},
        {"region": "대구광역시", "value": 21.0},
        {"region": "서울특별시", "value": 19.5},
    ],
    "highest": "부산광역시",
    "lowest": "서울특별시",
    "gap": 5.0,
    "gap_unit": "%p",
    "notes": [],
    **SRC,
}
PARTS = {
    "status": "ok",
    "indicator": "인구 구성",
    "unit": "%",
    "region": "서울특별시",
    "year": 2025,
    "sex": "계",
    "by": "age",
    "total": 1000,
    "parts": [
        {"label": "유소년인구(0~14세)", "count": 100, "share": 10.0},
        {"label": "생산연령인구(15~64세)", "count": 700, "share": 70.0},
        {"label": "고령인구(65세 이상)", "count": 200, "share": 20.0},
    ],
    **SRC,
}


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,]*(?:\.\d+)?", text))


def test_value_is_its_sentence() -> None:
    s = a.build(VALUE)
    assert s == {
        "headline": "2025년 부산광역시의 고령인구 비율은 24.5%입니다.",
        "points": [],
        "cautions": [],
    }


def test_trend_headline_is_the_whole_change() -> None:
    s = a.build(trend("대구광역시", [1000, 900, 810]))
    assert s["headline"] == (
        "대구광역시의 출생아 수는 2020~2022년에 190명 감소했습니다(1,000명 → 810명, -19.0%)."
    )
    # 매년 줄었으면 최고·최저 해는 처음과 끝이라 따로 적지 않는다
    assert s["points"] == [
        "이 기간 동안 매년 감소했습니다.",
        "해마다 평균 95명씩 줄었습니다((마지막 값 − 처음 값) ÷ 햇수).",
    ]


def test_trend_that_turns_names_the_year_and_extremes() -> None:
    s = a.build(trend("전국", [100, 80, 60, 70]))
    assert s["points"][0] == "2022년까지 감소하다가 이후 증가했습니다."
    assert s["points"][1] == "가장 많았던 해는 2020년(100명), 가장 적었던 해는 2022년(60명)입니다."


def test_trend_up_and_down_counts_the_years() -> None:
    s = a.build(trend("전국", [100, 110, 90, 95, 80]))
    assert s["points"][0] == "앞 해보다 증가한 해는 2번, 감소한 해는 2번입니다."


def test_trend_puts_what_was_asked_first() -> None:
    s = a.build(trend("전국", [100, 80, 60, 70], asked={"extreme": "min"}))
    assert s["points"][0] == "2020~2023년 중 출생아 수가 가장 적었던 해는 2022년(60명)입니다."
    assert not any(
        p.startswith("가장 많았던 해") for p in s["points"]
    )  # 같은 말을 두 번 하지 않는다


def test_trend_without_change_says_same() -> None:
    s = a.build(trend("전국", [100, 100]))
    assert s["headline"] == "전국의 출생아 수는 2020년과 2021년이 같습니다(100명 → 100명)."
    assert s["points"] == []


def test_region_trends_are_grouped_not_listed() -> None:
    parts = [
        trend("서울특별시", [1000, 950, 900]),
        trend("세종특별자치시", [100, 120, 150]),
        trend("부산광역시", [500, 480, 400]),
    ]
    s = a.build({"status": "ok", "split": parts, "split_by": "region", **SRC})
    assert s["headline"] == "2020~2022년에 출생아 수는 3곳 중 1곳이 증가, 2곳이 감소했습니다."
    assert s["points"] == [
        "가장 크게 늘어난 곳은 세종특별자치시(+50.0%), "
        "가장 크게 줄어든 곳은 부산광역시(-20.0%)입니다.",
        "2022년에는 서울특별시(900명)가 가장 많고, 세종특별자치시(150명)가 가장 적습니다.",
    ]


def test_region_trends_all_one_way() -> None:
    parts = [trend("서울특별시", [1000, 900]), trend("부산광역시", [500, 400])]
    s = a.build({"status": "ok", "split": parts, "split_by": "region", **SRC})
    assert s["headline"] == "2020~2021년에 출생아 수는 2곳 모두 감소했습니다."
    assert s["points"][0] == (
        "가장 크게 줄어든 곳은 부산광역시(-20.0%), 가장 적게 줄어든 곳은 서울특별시(-10.0%)입니다."
    )


def test_region_trends_with_different_periods_are_listed() -> None:
    parts = [
        trend("서울특별시", [1000, 900]),
        trend("부산광역시", [500, 400], start=2018),
        trend("대구광역시", [300, 200]),
    ]
    s = a.build({"status": "ok", "split": parts, "split_by": "region", **SRC})
    assert s["headline"].startswith("서울특별시의 출생아 수는 2020~2021년에")
    assert s["points"][0].startswith("부산광역시의 출생아 수는 2018~2019년에")


@pytest.mark.parametrize("end, word", [(110, "늘어난"), (90, "줄어든")])
@pytest.mark.parametrize("with_rate", [True, False])
def test_region_trends_exclude_unchanged_regions(end: int, word: str, with_rate: bool) -> None:
    parts = [trend("서울", [100, end]), trend("부산", [100, 100])]
    if not with_rate:
        for p in parts:
            p["change"].pop("rate")
    s = a.build(qa._combine(parts, by="region"))
    assert "1곳은 변동이 없습니다" in s["headline"]
    signed = f"{end - 100:+.1f}%" if with_rate else f"{end - 100:+}명"
    assert s["points"][0] == f"{word} 곳은 서울({signed})입니다."


@pytest.mark.parametrize("ends", [[110, 120, 100], [90, 80, 100], [110, 90, 100]])
def test_region_trend_extremes_only_name_changed_regions(ends: list[int]) -> None:
    parts = [
        trend(region, [100, end])
        for region, end in zip(("서울", "대구", "부산"), ends, strict=True)
    ]
    s = a.build(qa._combine(parts, by="region"))
    assert "서울" in s["points"][0] and "대구" in s["points"][0]
    assert "부산" not in s["points"][0]


def test_region_trends_all_unchanged_have_no_movement_point() -> None:
    parts = [trend("서울", [100, 100]), trend("부산", [100, 100])]
    s = a.build(qa._combine(parts, by="region"))
    assert "2곳은 변동이 없습니다" in s["headline"]
    assert s["points"] == ["2021년에는 2곳이 모두 같습니다."]


def test_nested_region_and_sex_trends_summarize_every_leaf() -> None:
    parts = [
        qa._combine([trend(region, [100, end], sex=sex) for sex, end in (("남", 110), ("여", 90))])
        for region in ("서울", "부산")
    ]
    s = a.build(qa._combine(parts, by="region"))
    expected = [a.build(p)["headline"] for part in parts for p in part["split"]]
    assert [s["headline"], *s["points"]] == expected


def test_nested_splits_with_two_leaves_share_the_headline() -> None:
    parts = [trend("서울", [100, 110]), trend("부산", [100, 90])]
    # 한 갈래만 있는 묶음도 중첩 깊이에 관계없이 처리한다.
    result = qa._combine([qa._combine([qa._combine([p])]) for p in parts])
    s = a.build(result)
    assert s["headline"] == " ".join(a.build(p)["headline"] for p in parts)
    assert s["points"] == []


def test_rank_adds_runners_up_and_the_other_end() -> None:
    s = a.build(RANK)
    assert s["headline"] == "2025년 고령인구 비율이 가장 높은 시도는 전라남도(27.2%)입니다."
    assert s["points"] == [
        "2~3위는 경상북도(26.04%), 부산광역시(24.5%)입니다.",
        "가장 낮은 곳은 세종특별자치시(11.6%)입니다.",
    ]


def test_rank_top_n_is_already_complete() -> None:
    s = a.build({**RANK, "ranking": RANK["ranking"][:3]})
    assert "3곳은 전라남도(27.2%), 경상북도(26.04%), 부산광역시(24.5%)" in s["headline"]
    assert s["points"] == []


def test_rank_of_one_region_adds_the_top() -> None:
    target = {"region": "부산광역시", "value": 24.5, "rank": 3}
    s = a.build({**RANK, "target": target})
    assert s["headline"].endswith("시도 4곳 중 높은 순으로 3위입니다.")
    assert s["points"] == ["1위는 전라남도(27.2%)입니다."]


def test_compare_lists_in_order_and_names_missing() -> None:
    s = a.build(COMPARE)
    assert s["headline"].endswith("차이는 5%p입니다.")
    assert s["points"] == [
        "높은 순으로 부산광역시(24.5%), 대구광역시(21%), 서울특별시(19.5%)입니다."
    ]
    missing = {
        **COMPARE,
        "values": [*COMPARE["values"], {"region": "세종특별자치시", "value": None}],
    }
    assert a.build(missing)["points"][-1] == "세종특별자치시의 자료는 없습니다."


def test_composition_leads_with_the_biggest_share() -> None:
    s = a.build(PARTS)
    assert s["headline"] == (
        "2025년 서울특별시 인구에서 가장 큰 비중은 생산연령인구(15~64세) 70.0%입니다."
    )
    assert s["points"] == [
        "나머지는 유소년인구(0~14세) 10.0%, 고령인구(65세 이상) 20.0%입니다.",
        "전체는 1,000명입니다.",
    ]


def test_composition_leads_with_the_asked_part() -> None:
    r = {
        **PARTS,
        "subject": "출생아",
        "parts": [
            {"label": "첫째", "count": 600, "share": 60.0},
            {"label": "둘째", "count": 300, "share": 30.0},
            {"label": "셋째 이상", "count": 100, "share": 10.0},
        ],
        "asked": {"focus": ["둘째"]},
    }
    s = a.build(r)
    assert s["headline"] == "2025년 서울특별시의 출생아 중 둘째는 30.0%(300명)입니다."
    assert s["points"][0] == "가장 큰 비중은 첫째 60.0%입니다."


def test_values_of_one_place_share_a_sentence() -> None:
    births = {**VALUE, "indicator": "출생아 수", "kind": "count", "unit": "명", "value": 300}
    deaths = {**births, "indicator": "사망자 수", "value": 500}
    s = a.build({"status": "ok", "split": [births, deaths], "split_by": "indicator", **SRC})
    assert s["headline"] == "2025년 부산광역시의 출생아 수는 300명, 사망자 수는 500명입니다."
    assert s["points"] == []  # 다른 지표끼리는 크기를 견주지 않는다


def test_sum_names_its_members() -> None:
    members = [{"region": "서울특별시", "value": 10}, {"region": "경기도", "value": 14.5}]
    s = a.build({**VALUE, "region": "서울특별시 + 경기도", "members": members})
    assert s["points"] == ["서울특별시 10%, 경기도 14.5%을 더한 값입니다."]


def test_cautions_carry_what_must_not_be_dropped() -> None:
    causal = "원인이나 정책 효과는 통계 수치만으로 판단할 수 없어 수치 변화만 보여드립니다."
    s = a.build({**RANK, "causal_question": causal, "notes": ["2023년: 군위군이 대구로 편입"]})
    assert s["cautions"] == [causal, "2023년: 군위군이 대구로 편입."]
    assert causal not in s["headline"]


def test_projection_instead_of_out_of_range_year() -> None:
    suggestion = {
        "status": "ok",
        "indicator": "총인구",
        "region": "서울특별시",
        "year": 2040,
        "value": 8709899.0,
        "unit": "명",
        "scenario": "중위 추계",
        **SRC,
    }
    s = a.build({"status": "out_of_range", "reason": "수록 기간 밖", "suggestion": suggestion})
    assert s["headline"] == (
        "2040년 서울특별시의 총인구 장래인구추계(중위 추계) 값은 8,709,899명입니다."
    )
    assert s["cautions"] == [
        "물은 연도는 수록 기간 밖이라 장래인구추계 값으로 답했습니다.",
        "실제 통계가 아닌 추계값입니다.",
    ]


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"status": "out_of_range", "reason": "수록 기간 밖"},
        {"status": "unknown_indicator"},
        {"status": "ok", "reused": True} | {"status": "tables"},
    ],
)
def test_no_summary_without_numbers(result: dict | None) -> None:
    assert a.build(result) is None


@pytest.mark.parametrize(
    "result",
    [
        VALUE,
        RANK,
        COMPARE,
        PARTS,
        trend("전국", [100, 110, 90, 95, 80]),
        {
            "status": "ok",
            "split_by": "region",
            "split": [
                trend("서울특별시", [1000, 950, 900]),
                trend("세종특별자치시", [100, 120, 150]),
            ],
        },
    ],
)
def test_summary_adds_no_number_the_answer_lacks(result: dict) -> None:
    """요약의 숫자는 답변(문장 + 수치 목록)에 이미 있는 숫자다.
    요약이 새로 만드는 것은 개수(곳·번·위)와 해마다 평균뿐이다"""
    s = a.build(result)
    shown = numbers(f"{qa.summary(result)}\n{qa.render(result)}")
    made = re.compile(r"\d+곳|\d+번|\d+~\d+위|해마다 평균 [\d,.]+")
    text = made.sub("", a.text(s))
    assert numbers(text) <= shown, numbers(text) - shown


def test_text_has_no_blank_line_and_one_line_is_the_headline() -> None:
    s = a.build({**RANK, "notes": ["주의"]})
    text = a.text(s)
    assert text.split("\n") == [s["headline"], *(f"- {p}" for p in s["points"]), "※ 주의."]
    assert a.text(s, one_line=True) == s["headline"]
    assert a.one_line("한 줄로 요약해줘") and not a.one_line("요약해줘")
    assert a.one_line("한마디로 하면?") and a.one_line("한 문장으로 말해줘")


def test_parallel_trends_share_one_short_sentence() -> None:
    """남편·아내처럼 따로 공표되는 지표의 추이: 갈래마다 문장을 잇지 않는다 (전에는 150자 넘음)"""
    husband = trend("서울특별시", [33.0, 33.5, 34.0], indicator="평균 초혼연령(남편)", unit="세")
    wife = trend("서울특별시", [31.0, 31.2, 31.8], indicator="평균 초혼연령(아내)", unit="세")
    for r in (husband, wife):
        r["kind"], r["change"]["diff_unit"] = "value", "세"
    s = a.build({"status": "ok", "split": [husband, wife], **SRC})
    assert s["headline"] == (
        "서울특별시의 평균 초혼연령(남편)은 2020~2022년에 1세 증가, "
        "평균 초혼연령(아내)은 0.8세 증가했습니다."
    )
    assert s["points"] == [
        "평균 초혼연령(남편): 33세 → 34세, +3.03%",
        "평균 초혼연령(아내): 31세 → 31.8세, +2.58%",
    ]
    assert a.text(s, one_line=True) == s["headline"]


def test_two_long_sentences_are_not_joined_into_the_headline() -> None:
    men = {**COMPARE, "indicator": "일반혼인율(남자)"}
    women = {**COMPARE, "indicator": "일반혼인율(여자)"}
    s = a.build({"status": "ok", "split": [men, women], **SRC})
    assert s["headline"].startswith("2025년 일반혼인율(남자)이 가장 높은 지역은")
    assert len(s["headline"]) <= a.MAX_HEADLINE
    assert s["points"][0].startswith("2025년 일반혼인율(여자)이 가장 높은 지역은")
