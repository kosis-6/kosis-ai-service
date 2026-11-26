"""숫자 구간 축의 구간 합 (band): 칸 이름 · 질문 범위 읽기, 칸 합치기, 정의어 (DB · LLM 없음)."""

from app.services import band
from app.services.dynamic_indicator import Dim

WEIGHT = Dim(
    4,
    "출생시 체중별",
    {
        "00": "계",
        "10": "1.5㎏미만",
        "20": "1.5 - 1.9㎏",
        "30": "2.0 - 2.4㎏",
        "40": "2.5 - 2.9㎏",
        "50": "3.0 - 3.4㎏",
        "60": "3.5 - 3.9㎏",
        "70": "4.0 - 4.4㎏",
        "80": "4.5㎏이상",
        "99": "미상",
    },
)
COHABIT = Dim(
    2,
    "동거기간별",
    {
        "00": "계",
        "01": "0∼4년",
        "02": "5∼9년",
        "03": "10∼14년",
        "04": "15∼19년",
        "05": "20년이상",
        "06": "20∼24년",
        "07": "25∼29년",
        "08": "30년이상",
        "99": "미상",
    },
)
MOTHER = Dim(
    3,
    "모의 연령(5세계급)별",
    {
        "00": "계",
        "16": "15세미만",
        "20": "15 - 19세",
        "35": "30 - 34세",
        "40": "35 - 39세",
        "45": "40 - 44세",
        "50": "45 - 49세",
        "56": "50세이상",
        "95": "연령미상",
    },
)


def test_labels_become_bands() -> None:
    assert band.parse_label("1.5㎏미만") == band.Band(None, 1.5, "kg", open_hi=True)
    assert band.parse_label("2.0 - 2.4㎏") == band.Band(2.0, 2.4, "kg")
    assert band.parse_label("4.5㎏ 이상") == band.Band(4.5, None, "kg")
    assert band.parse_label("20년이상") == band.Band(20, None, "년")
    assert band.parse_label("0∼4년") == band.Band(0, 4, "년")
    assert band.parse_label("미상") is None and band.parse_label("계") is None


def test_question_ranges_are_read() -> None:
    assert band.parse_ask("2.5kg 미만 아기")[0] == (
        band.Band(None, 2.5, "kg", open_hi=True),
        "2.5kg 미만",
    )
    assert band.parse_ask("4kg 이상")[0][0] == band.Band(4, None, "kg")
    assert band.parse_ask("2.5~3.9kg")[0][0] == band.Band(2.5, 3.9, "kg")
    assert band.parse_ask("20년 넘게 같이 산")[0][0] == band.Band(20, None, "년")


def test_union_sums_the_cells_a_range_covers() -> None:
    assert band.union_codes(WEIGHT, band.Band(None, 2.5, "kg", open_hi=True)) == ["10", "20", "30"]
    assert band.union_codes(WEIGHT, band.Band(4, None, "kg")) == ["70", "80"]
    assert band.union_codes(WEIGHT, band.Band(2.5, 3.9, "kg")) == ["40", "50", "60"]
    # 2.7kg 미만: 2.5-2.9 칸이 경계를 걸침 → 못 냄
    assert band.union_codes(WEIGHT, band.Band(None, 2.7, "kg", open_hi=True)) is None
    # 단위가 다르면 못 냄
    assert band.union_codes(WEIGHT, band.Band(None, 2.5, "g", open_hi=True)) is None


def test_overlapping_cells_prefer_the_wide_one() -> None:
    assert band.union_codes(COHABIT, band.Band(20, None, "년")) == ["05"]
    assert band.union_codes(COHABIT, band.Band(25, None, "년")) == ["07", "08"]
    assert band.union_codes(COHABIT, band.Band(None, 9, "년")) == ["01", "02"]


def test_mother_age_definition_word(monkeypatch) -> None:
    monkeypatch.setattr(
        band,
        "words",
        lambda: (
            band.Word("고령산모", "모의 연령", "35세 이상"),
            band.Word("저체중아", "체중", "2.5kg 미만"),
        ),
    )
    found = band.find(["고령산모 비중"], [MOTHER, WEIGHT], None)
    assert found == [band.Union(3, ("40", "45", "50", "56"), "고령산모(35세 이상)")]
    found = band.find(["저체중아 출산 비중"], [MOTHER, WEIGHT], None)
    assert found == [band.Union(4, ("10", "20", "30"), "저체중아(2.5kg 미만)")]
    # 사람 연령(35세 이상 인구)은 여기서 잇지 않는다 (lookup._age_codes 의 몫)
    assert band.find(["35세 이상 인구"], [MOTHER, WEIGHT], None) == []
    # 정의어가 없는 표에서는 아무것도 잇지 않는다
    assert band.find(["고령산모 비중"], [WEIGHT], None) == []
