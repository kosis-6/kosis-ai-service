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


def _scope(lines: list[str]) -> list[str]:
    return nt.scope({"comments": [{"target": "통계표", "lines": lines}]})


def test_scope_states_who_is_counted_and_excluded() -> None:
    assert _scope(
        [
            "주1) 일반가구의 가구원(일반가구원)을 대상으로 집계(혼인상태는 15세 이상 인구를 대상).",
            "단, 집단가구(비친족 6인 이상 가구), 특별조사구(군인 등) 및 외국인가구 제외",
            "주2) 내국인(한국인)과 함께 살고 있는 외국인은 일반가구원으로 집계",
            "※ 2025년 주요변경사항",
            "ㅇ 시군구→읍면동 확대 공표 : 6개 통계표",
            "- 가구형태별 가구 및 가구원 - 읍면동(연도 끝자리 0, 5), 시군구(그 외 연도)",
        ]
    ) == [
        "일반가구의 가구원(일반가구원)을 대상으로 집계(혼인상태는 15세 이상 인구를 대상).",
        "단, 집단가구(비친족 6인 이상 가구), 특별조사구(군인 등) 및 외국인가구 제외",
    ]


def test_scope_joins_lines_under_the_header() -> None:
    assert _scope(
        ["○ 집계대상", "- 일반가구", "◦ 집단가구, 집단시설가구, 외국인가구 및 특별조사구 제외"]
    ) == ["집계 대상: 일반가구 · 집단가구, 집단시설가구, 외국인가구 및 특별조사구 제외"]


def test_scope_skips_common_only_and_other_kinds_of_lines() -> None:
    # 거의 모든 총조사 표에 붙는 문구뿐이면 붙이지 않는다
    assert _scope(["외국인가구 및 특별조사구 제외"]) == []
    assert _scope(["특별조사구 인구 제외."]) == []
    # 연도가 든 줄('조사 제외'가 맡는다), 행정구역 변경, 표기 방법, 용어 뜻풀이는 대상 범위가 아니다
    assert _scope(
        [
            "새터민은 조사 제외(2007~2008)",
            "97.7.15 울주구(농소읍, 강동면 제외)가 울주군으로 명칭 변경",
            "주3) 개인정보 보호를 위해 5미만 자료는 X로 표기함",
            "내국인(귀화·인지) : 국적법상 귀화·인지에 의한 국적취득자(국적회복자 제외)",
        ]
    ) == []
    assert _scope(["연말기준, 주민등록에 의한 집계, 외국인 제외"]) == [
        "연말기준, 주민등록에 의한 집계, 외국인 제외"
    ]


def test_scope_joins_a_sentence_broken_across_lines() -> None:
    broken = [
        "집계대상 : 상주인구는 일반가구 및 집단가구(6인이상 비친족가구, 기숙사,",
        "사회시설 등) 및 외국인가구 제외",
    ]
    assert _scope(broken) == [
        "집계 대상: 상주인구는 일반가구 및 집단가구(6인이상 비친족가구, 기숙사, "
        "사회시설 등) 및 외국인가구 제외"
    ]


def test_scope_comes_right_after_the_basis_line() -> None:
    table = _table(
        target=None,
        lines=["※ 2015년 1월1일 기준", "*은 5인 미만인 경우임", "국내 거주 외국인을 대상으로 집계"],
    )
    assert nt.notices(table, [], _slots()) == [
        "2015년 1월1일 기준",
        "국내 거주 외국인을 대상으로 집계",
        "*은 5인 미만인 경우임",
    ]

