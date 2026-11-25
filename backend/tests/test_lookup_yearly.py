import inspect
import re
import sqlite3
from contextlib import closing

import pytest

from app.services import dynamic_indicator, indicators, lookup


def test_item_years_reads_yearly_rows_only(monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(lookup, "_in", lambda *a: ("item_key in (?)", [1]))
    monkeypatch.setattr(lookup, "_tbl_key", lambda tbl: 7)

    def rows(sql, params):
        seen.append(sql)
        return [{"lo": 1, "hi": 2}]

    monkeypatch.setattr(lookup, "_rows", rows)
    lookup._item_years_db.__wrapped__("DT_TEST", ("T1",))
    assert lookup.YEARLY in seen[0]


def test_every_stat_value_query_filters_period_type() -> None:
    # stat_value를 읽는 쿼리를 새로 만들 때 시점 종류 조건(YEARLY, 달 묶음 합은 MONTHLY)을
    # 빠뜨리지 않게 한다
    source = inspect.getsource(lookup)
    queries = len(re.findall(r"from stat_value", source))
    filtered = re.findall(r"\{(?:YEARLY|MONTHLY|MONTHLY if monthly else YEARLY)\}", source)
    assert queries == len(filtered) >= 3


@pytest.mark.parametrize(
    ("region_code", "source_code", "region", "level", "expected"),
    [
        (None, None, "전국", "전국", None),
        ("00", "total", "전국", "전국", 6792),
        ("11", "local1", "서울특별시", "시도", 2396),
        ("11110", "local1", "종로구", "시군구", 2396),
    ],
)
def test_value_uses_only_mapped_regions_when_region_axis_exists(
    monkeypatch, region_code, source_code, region, level, expected
) -> None:
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            create table stat_value (
                tbl_key int, period int, prd_se text, item_key int, c1 int, value int
            );
            create table stat_dimension (tbl_key int, dim_no int, name text);
            create table stat_code (tbl_key int, dim_no int, code_key int, code text, name text);
            create table region_code_map (
                tbl_id text, src_dim int, src_code text, region_code text,
                valid_from int, valid_to int
            );
            insert into stat_dimension values (7, 0, '항목'), (7, 1, '지역');
            insert into stat_code values
                (7, 0, 1, 'T10', '인구'), (7, 1, 10, 'total', '전국'),
                (7, 1, 11, 'local1', '지역1'), (7, 1, 12, 'local2', '지역2');
            insert into stat_value values
                (7, 2018, 'A', 1, 10, 6792),
                (7, 2018, 'A', 1, 11, 2396),
                (7, 2018, 'A', 1, 12, 4396);
        """)
        if region_code is not None:
            db.execute(
                "insert into region_code_map values ('DT_TEST', 1, ?, ?, null, null)",
                (source_code, region_code),
            )

        def rows(sql, params):
            return db.execute(sql.replace("::float8", ""), params).fetchall()

        regions = {
            "전국": ("00", "전국"),
            "서울특별시": ("11", "시도"),
            "종로구": ("11110", "시군구"),
            "부산광역시": ("21", "시도"),
        }

        def resolve(spec, names, year):
            return [
                {"display": name, "kind": regions[name][1], "region_code": regions[name][0]}
                for name in names
            ]

        monkeypatch.setattr(lookup, "_rows", rows)
        monkeypatch.setattr(dynamic_indicator, "_rows", rows)
        monkeypatch.setattr(lookup, "_tbl_key", lambda tbl: 7)
        monkeypatch.setattr(lookup, "_codes_db", lookup._codes_db.__wrapped__)
        monkeypatch.setattr(lookup, "_region_dim_db", lookup._region_dim_db.__wrapped__)
        monkeypatch.setattr(lookup, "_item_years_db", lookup._item_years_db.__wrapped__)
        monkeypatch.setattr(lookup, "_projection", lambda tbl: False)
        monkeypatch.setattr(lookup, "_resolve", resolve)
        metadata = {"tbl_nm": "인구", "org_nm": "시험청", "basis": ""}
        monkeypatch.setattr(lookup, "_table", lambda tbl: metadata)
        monkeypatch.setattr(dynamic_indicator, "is_projection", lambda tbl: False)
        monkeypatch.setattr(dynamic_indicator, "_unit", lambda *args: ("명", True))
        monkeypatch.setattr(indicators, "canonical_name", lambda name: None)
        monkeypatch.setattr(indicators, "find_regions", lambda text: [])
        monkeypatch.setattr(indicators, "kosis_url", lambda tbl: None)
        parsed = {"type": "value", "regions": [region], "year": 2018, "indicator": "인구"}
        spec, why = dynamic_indicator.build("DT_TEST", f"2018년 {region} 인구", parsed)
        assert spec, why
        src = spec["sources"][0]
        assert src["level"] == level and bool(src.get("national_only")) == (level == "전국")
        assert lookup.value(spec, region, 2018)["value"] == expected
        if level != "전국":
            assert lookup.value(spec, "전국", 2018)["value"] is None
            assert lookup.value(spec, "부산광역시", 2018)["value"] is None
