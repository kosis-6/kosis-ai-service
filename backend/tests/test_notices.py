from app.services import notices as nt
from app.services import slots as sl

LINES = [
    "주1) 혼인귀화자 : 내국인과 혼인을 목적으로 입국하여 국적을 취득한 자",
    "2010년 7월 1일자로 마산시, 진해시는 창원시로 통합",
    "2012년 1월 1일자로 당진군이 당진시로 승격",
    "※ 2015년 1월1일 기준",
    "*은 5인 미만인 경우임",
    "새터민은 조사 제외(2007~2008)",
    "ㅇ 시군구→읍면동 확대 공표 : 6개 통계표",
]


def _table(target: str | None = "혼인 귀화자", lines: list[str] = LINES) -> dict:
    return {"target": target, "comments": [{"target": "통계표", "lines": lines}]}


def _slots(**cond) -> dict:
    s = sl.empty_slots("q")
    s["conditions"].update(cond)
    return s


def test_clean_strips_markers() -> None:
    assert nt.clean("주3) 중국인은 …") == "중국인은 …"
    assert nt.clean("※ 2015년 1월1일 기준") == "2015년 1월1일 기준"
    assert nt.clean("*은 5인 미만인 경우임") == "*은 5인 미만인 경우임"
    assert nt.clean("(**)<b><a href='x'>") == "(**)"


def test_always_basis_and_missing() -> None:
    assert nt.notices(_table(target=None), [], _slots()) == [
        "2015년 1월1일 기준",
        "*은 5인 미만인 경우임",
    ]


def test_definition_found_in_other_edition() -> None:
    new = _table(lines=[])  # 새 판에는 정의가 없다
    old = _table()
    assert nt.notices(new, [new, old], _slots()) == [
        "혼인귀화자 : 내국인과 혼인을 목적으로 입국하여 국적을 취득한 자"
    ]
    # '시군구→읍면동 확대 공표 : …'처럼 대상이 아닌 줄은 정의로 보지 않는다
    assert nt.definitions([_table()], "전체") == []


def test_region_and_year_only_when_asked() -> None:
    t = _table(target=None)
    assert "2010년 7월 1일자로 마산시, 진해시는 창원시로 통합" in nt.notices(
        t, [t], _slots(region="경상남도 창원시")
    )
    assert not any("통합" in n for n in nt.notices(t, [t], _slots(region="서울특별시")))
    assert "새터민은 조사 제외(2007~2008)" in nt.notices(t, [t], _slots(year=2008))
    assert not any("제외" in n for n in nt.notices(t, [t], _slots(year=2010)))
