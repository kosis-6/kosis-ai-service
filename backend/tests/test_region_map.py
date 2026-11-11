"""표 하나의 지역 대응 (pipeline/region_map.py): 지역 칸 찾기, 기준 지역 연결 규칙 (DB 없이)"""

import pytest

from pipeline.region_map import (
    RegionIndex,
    Source,
    find_region_dim,
    map_rows,
    match_region,
    unmatched_reason,
)

REGIONS = [
    ("00", "전국", "전국", "전국"),
    ("11", "서울특별시", "시도", "서울특별시"),
    ("11680", "강남구", "시군구", "서울특별시 > 강남구"),
    ("31", "울산광역시", "시도", "울산광역시"),
    ("41", "경기도", "시도", "경기도"),
    ("41195", "원미구", "일반구", "경기도 > 원미구"),
    ("41192", "원미구", "일반구", "경기도 > 원미구"),
    ("51", "강원특별자치도", "시도", "강원특별자치도"),
    ("51110", "춘천시", "시군구", "강원특별자치도 > 춘천시"),
]


@pytest.fixture
def idx() -> RegionIndex:
    return RegionIndex(REGIONS)


@pytest.mark.parametrize(
    ("dims", "expected"),
    [
        ([(1, "행정구역별"), (2, "성별")], 1),
        ([(1, "성별"), (2, "시도별")], 2),
        ([(1, "연령별"), (2, "성별"), (3, "거주지별")], 3),
        ([(1, "현거주지별"), (2, "5년전 거주지별")], 1),  # 앞 칸
        ([(2, "행정구역별(시군구)"), (1, "성별")], 2),  # 순서 섞여도 칸 번호 순
        ([(1, "전출지별"), (2, "전입지별")], 1),
        ([(1, "국적(지역)별"), (2, "성별")], None),  # 국외
        ([(1, "특성별"), (2, "성별")], None),
        ([], None),
    ],
)
def test_find_region_dim(dims, expected) -> None:
    assert find_region_dim("DT_TEST", dims) == expected


def test_find_region_dim_override() -> None:
    # 장래추계는 c1이 시나리오, c2가 지역
    assert find_region_dim("DT_1BPB002", [(1, "시나리오별"), (2, "시도별")]) == 2
    # 지역 분류 없는 표로 정해 둔 표는 이름과 상관없이 None
    assert find_region_dim("INH_1B8000F_01", [(1, "행정구역별")]) is None


def test_total(idx) -> None:
    assert match_region(idx, "101", Source("00", "전국", 1, "전국")) == (["00"], "total")
    # 1단계가 아닌 '계'는 전국이 아님
    assert match_region(idx, "101", Source("X", "계", 2, "서울특별시 > 계"))[0] != ["00"]


def test_code_needs_same_name(idx) -> None:
    assert match_region(idx, "101", Source("11680", "강남구", 2, None)) == (["11680"], "code")
    # 예전 코드 체계: 31 = 경기도 (현재 31 = 울산광역시) → 코드로 잇지 않고 이름으로
    assert match_region(idx, "101", Source("31", "경기도", 1, "경기도")) == (["41"], "sido")


def test_code_only_for_statistics_korea(idx) -> None:
    # 행정안전부(110) 표는 코드가 같아도 경로로 확인
    src = Source("11680", "강남구", 2, "서울특별시 > 강남구")
    assert match_region(idx, "110", src) == (["11680"], "path")


def test_path_keeps_all_candidates(idx) -> None:
    src = Source("A1", "원미구", 2, "경기도 > 부천시 > 원미구")
    codes, method = match_region(idx, "110", src)
    assert method == "path" and sorted(codes) == ["41192", "41195"]


def test_old_sido_name(idx) -> None:
    src = Source("B1", "춘천시", 2, "강원도 > 춘천시")
    assert match_region(idx, "110", src) == (["51110"], "path")
    assert match_region(idx, "110", Source("B0", "강원도", 1, "강원도")) == (["51"], "sido")


def test_unmatched(idx) -> None:
    assert match_region(idx, "101", Source("Z", "국외", 1, "국외")) == ([], None)
    assert unmatched_reason("국외") == "국외"
    assert unmatched_reason("동부") == "지역이 아닌 동·읍·면부 분류"
    assert unmatched_reason("원미구(변동전)").startswith("변동전")
    assert unmatched_reason("제물포구").startswith("기준 지역 목록")


def test_map_rows(idx) -> None:
    sources = [
        Source("00", "전국", 1, "전국"),
        Source("A1", "원미구", 2, "경기도 > 부천시 > 원미구"),
        Source("Z", "국외", 1, "국외"),
    ]
    rows = map_rows(idx, "DT_TEST", "110", 2, sources)
    assert [(r["src_code"], r["region_code"], r["match_method"]) for r in rows] == [
        ("00", "00", "total"),
        ("A1", "41195", "path"),
        ("A1", "41192", "path"),
        ("Z", None, None),
    ]
    assert all(r["tbl_id"] == "DT_TEST" and r["src_dim"] == 2 for r in rows)
    assert rows[-1]["unmatched_reason"] == "국외"
