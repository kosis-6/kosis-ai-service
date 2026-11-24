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


@pytest.mark.needs_data
def test_rebuild_matches_preprocess_rule(pg) -> None:
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
