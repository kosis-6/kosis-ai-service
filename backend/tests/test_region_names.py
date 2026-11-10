"""지역 이름 해석: 시군구 짧은 이름("강남" → 강남구)과 권역 묶음(부울경 등)

DB 없이 돌리기 위해 indicators._rows를 작은 지역 목록으로 바꿔 끼운다.
"""

import pytest

from app.services import indicators, qa

REGIONS = [
    # (코드, 이름, 종류, 시도 코드, 시도 이름)
    ("11", "서울특별시", "시도", None, None),
    ("26", "부산광역시", "시도", None, None),
    ("29", "광주광역시", "시도", None, None),
    ("41", "경기도", "시도", None, None),
    ("41610", "광주시", "시군구", "41", "경기도"),
    ("11680", "강남구", "시군구", "11", "서울특별시"),
    ("11500", "강서구", "시군구", "11", "서울특별시"),
    ("26440", "강서구", "시군구", "26", "부산광역시"),
    ("26350", "해운대구", "시군구", "26", "부산광역시"),
]
# 시도 별칭 (preprocess.SIDO_ALIASES 일부)
ALIASES = {"서울": "11", "부산": "26", "광주": "29", "경기": "41"}


def _fake_rows(sql, params=None):
    if "region_aliases" not in sql or not params:
        return []
    key = params[0]
    if key in ALIASES:
        key = ALIASES[key]
    return [
        {
            "region_code": c,
            "region_name": n,
            "kind": k,
            "sido_code": sc,
            "sido_name": sn,
            "valid_from": None,
            "valid_to": None,
        }
        for c, n, k, sc, sn in REGIONS
        if key in (n, c) and ("sido_code = ?" not in sql or sc == params[2])
    ]


@pytest.fixture(autouse=True)
def fake_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(indicators, "_rows", _fake_rows)


def test_short_gu_name() -> None:
    r = indicators.resolve_region("강남")
    assert (r["status"], r["region_code"]) == ("ok", "11680")
    assert indicators.resolve_region("해운대")["display"] == "부산광역시 해운대구"


def test_short_gu_name_ambiguous() -> None:
    r = indicators.resolve_region("강서")
    assert r["status"] == "ambiguous_region"
    assert r["candidates"] == ["서울특별시 강서구", "부산광역시 강서구"]


def test_full_name_still_works() -> None:
    assert indicators.resolve_region("강남구")["region_code"] == "11680"
    assert indicators.resolve_region("광주")["region_code"] == "29"


def test_sido_plus_short_name() -> None:
    assert indicators.resolve_region("경기 광주")["region_code"] == "41610"
    assert indicators.resolve_region("서울 강남")["region_code"] == "11680"
    assert indicators.resolve_region("부산 강서")["region_code"] == "26440"


@pytest.mark.parametrize(
    ("question", "members"),
    [
        ("부울경 인구 합계", ["부산", "울산", "경남"]),
        ("동남권 고령인구 비율", ["부산", "울산", "경남"]),
        ("대경권 출생아 수", ["대구", "경북"]),
    ],
)
def test_region_group_words(question: str, members: list[str]) -> None:
    parsed = {"type": "value"}
    fixed = {}

    def fix(key, value, reason):
        fixed[key] = value
        parsed[key] = value

    qa._read_question(question, parsed, fix)
    assert fixed["regions"] == members
