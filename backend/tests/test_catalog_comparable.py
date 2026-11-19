"""두 표의 값을 직접 비교해도 되는지 (catalog.comparable, 답변 불가 사례 3: 단위·조사 기준 차이)

표 설명서의 비교 그룹이 둘 다 있으면 그룹으로, 아니면 조사 이름으로 본다.
판단 규칙은 DB 조회를 바꿔 끼워 보고, 실제 표 몇 개는 DB로 본다 (적재 안 됐으면 건너뛴다).
"""

import pytest

from app.core.stats_db import StatsNotLoadedError
from app.services import catalog

SURVEYS = {
    "REG": "주민등록인구현황",
    "CENSUS_A": "인구총조사",
    "CENSUS_B": "인구총조사",
    "BIRTH": "인구동향조사",
    "BIRTH_MC": "인구동향조사",
    "BIRTH_X": "인구동향조사",
    "PROJ": "장래인구추계",
}
GROUPS = {
    "REG": ("resident_registration", "주민등록인구"),
    "BIRTH": ("vital_statistics", "인구동향조사(출생·사망·혼인·이혼)"),
    "BIRTH_MC": ("vital_statistics_multicultural", "다문화 인구동태"),
    "PROJ": ("projection", "장래인구추계"),
}
COMPATIBLE = {frozenset({"vital_statistics", "vital_statistics_multicultural"})}


@pytest.fixture
def notes(monkeypatch: pytest.MonkeyPatch) -> list:
    """조사 이름 · 비교 그룹 조회를 가짜 표로. 조회한 표 목록을 돌려준다"""
    asked: list = []

    def surveys(ids):
        asked.append(list(ids))
        return {t: SURVEYS[t] for t in ids if t in SURVEYS}

    monkeypatch.setattr(catalog, "surveys", surveys)
    monkeypatch.setattr(catalog, "_groups", lambda ids: {t: GROUPS[t] for t in ids if t in GROUPS})
    monkeypatch.setattr(catalog, "_compatible", lambda a, b: frozenset({a, b}) in COMPATIBLE)
    return asked


def test_same_table_needs_no_lookup(notes) -> None:
    assert catalog.comparable("REG", "REG")["comparable"] is True
    assert notes == []


def test_census_and_registration_are_not_compared(notes) -> None:
    # 주민등록 표는 비교 그룹이 있고 인구총조사 표는 없다 → 조사 이름으로 본다
    got = catalog.comparable("REG", "CENSUS_A")
    assert got["comparable"] is False
    assert got["surveys"] == ["주민등록인구현황", "인구총조사"]
    assert "직접 비교하지 않습니다" in got["reason"]


def test_census_tables_of_different_years_are_compared(notes) -> None:
    # 인구총조사는 조사 연도마다 표가 따로 있다. 같은 조사라 비교해도 된다
    assert catalog.comparable("CENSUS_A", "CENSUS_B")["comparable"] is True


def test_groups_decide_when_both_tables_have_one(notes) -> None:
    # 출생 통계와 다문화 출생 통계는 그룹이 다르지만 compatible_groups에 적어 둔 조합
    assert catalog.comparable("BIRTH", "BIRTH_MC")["comparable"] is True
    # 그룹을 적지 않은 같은 조사의 표와는 조사 이름으로 본다
    assert catalog.comparable("BIRTH", "BIRTH_X")["comparable"] is True


def test_different_groups_name_the_groups(notes) -> None:
    got = catalog.comparable("REG", "PROJ")
    assert got["comparable"] is False and got["surveys"] == ["주민등록인구", "장래인구추계"]
    assert "추계값" in got["reason"]


def test_unknown_table_is_undecided(notes) -> None:
    # 카탈로그에 없는 표 (면적 표 등): 판단할 수 없음. 부르는 쪽은 막지 않는다
    assert catalog.comparable("REG", "AREA")["comparable"] is None
    assert catalog.comparable("REG", None)["comparable"] is None


def _loaded() -> bool:
    try:
        return len(catalog.surveys(["DT_1B04006", "DT_1B08024", "DT_1IN1005"])) == 3
    except StatsNotLoadedError:
        return False


@pytest.mark.skipif(not _loaded(), reason="카탈로그(table_meta) 적재 안 됨")
def test_real_tables() -> None:
    # 주민등록인구(DT_1B04006)와 인구총조사 가구(DT_1B08024)는 직접 비교하지 않는다
    got = catalog.comparable("DT_1B04006", "DT_1B08024")
    assert got["comparable"] is False and got["surveys"] == ["주민등록인구현황", "인구총조사"]
    # 인구총조사 표끼리는 비교한다
    assert catalog.comparable("DT_1B08024", "DT_1IN1005")["comparable"] is True
    # 출생 통계(DT_1B8000H)와 다문화 출생 통계(DT_1BB0006): compatible_groups
    assert catalog.comparable("DT_1B8000H", "DT_1BB0006")["comparable"] is True
