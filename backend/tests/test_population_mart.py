"""갱신 뒤 mart.population 다시 만들기 (DB의 실제 적재본으로 확인, 바꾸지 않고 되돌린다)"""

import psycopg
import pytest

from pipeline import population_mart
from pipeline.stats_build import conninfo


@pytest.fixture
def pg():
    try:
        conn = psycopg.connect(conninfo(), autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("DB에 연결할 수 없음")
    with conn:
        yield conn


def test_other_tables_do_nothing(pg) -> None:
    assert population_mart.rebuild(pg, "DT_1B26001") is False


def test_rebuild_matches_preprocess_rule(pg) -> None:
    # 같은 규칙: raw.stat_value에서 다시 만든 결과가 지금 표와 같아야 한다 (트랜잭션 안에서 비교)
    with pg.transaction():
        pg.execute(population_mart.BUILD, [population_mart.TABLE])
        diff = pg.execute(
            "SELECT (SELECT count(*) FROM (SELECT * FROM population_new"
            " EXCEPT SELECT * FROM mart.population) a),"
            " (SELECT count(*) FROM (SELECT * FROM mart.population"
            " EXCEPT SELECT * FROM population_new) b)"
        ).fetchone()
        if not pg.execute("SELECT count(*) FROM population_new").fetchone()[0]:
            pytest.skip("주민등록인구 표가 적재되지 않음")
    assert diff == (0, 0)
