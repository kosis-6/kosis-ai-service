"""조회 지표는 연 단위 시점만 읽는다. 표 전체를 적재하면 월·분기 행(시점 202401 등)이 같은 표에
섞여 들어와, 거르지 않으면 수록 기간이 2000~202402년처럼 잡혀 정상 질문이 답변 불가가 된다
(2026-11-09 로컬 DB에 월 행을 넣어 확인: 조건 없이 5,438개 중 4,522개 결과가 바뀜)"""

import inspect
import re
import sqlite3
from contextlib import closing

from app.services import lookup


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
    # stat_value를 읽는 쿼리를 새로 만들 때 YEARLY 조건을 빠뜨리지 않게 한다
    source = inspect.getsource(lookup)
    queries = len(re.findall(r"from stat_value", source))
    assert queries == len(re.findall(r"\{YEARLY\}", source)) >= 3


def test_national_only_table_with_region_axis_uses_mapped_total(monkeypatch) -> None:
    # 전국 6,792와 하위 행 2,396 + 4,396을 같이 적재하지만 전국 코드만 매핑한다.
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.executescript("""
            create table stat_value (
                tbl_key int, period int, prd_se text, item_key int, c1 int, value int
            );
            create table stat_code (code_key int, code text);
            create table region_code_map (
                tbl_id text, src_dim int, src_code text, region_code text,
                valid_from int, valid_to int
            );
            insert into stat_code values (10, 'total'), (11, 'local1'), (12, 'local2');
            insert into stat_value values
                (7, 2018, 'A', 1, 10, 6792),
                (7, 2018, 'A', 1, 11, 2396),
                (7, 2018, 'A', 1, 12, 4396);
            insert into region_code_map values ('DT_TEST', 1, 'total', '00', null, null);
        """)

        def rows(sql, params):
            return db.execute(sql.replace("::float8", ""), params).fetchall()

        monkeypatch.setattr(lookup, "_rows", rows)
        monkeypatch.setattr(lookup, "_tbl_key", lambda tbl: 7)
        monkeypatch.setattr(lookup, "_in", lambda *a: ("v.item_key in (?)", [1]))
        monkeypatch.setattr(lookup, "_region_dim", lambda tbl: 1)
        monkeypatch.setattr(lookup, "_projection", lambda tbl: False)
        src = {"table": "DT_TEST", "item_id": "T10", "national_only": True}
        assert lookup._fetch(src, ("00",), 2018, 2018) == {(2018, "00"): 6792}
