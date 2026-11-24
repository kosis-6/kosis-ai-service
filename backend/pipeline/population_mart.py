import logging

import psycopg

logger = logging.getLogger(__name__)

TABLE = "DT_1B04006"

BUILD = """
CREATE TEMP TABLE population_new ON COMMIT DROP AS
SELECT v.period AS year,
       c1.code AS region_code,
       CASE i.code WHEN 'T2' THEN '계' WHEN 'T3' THEN '남' WHEN 'T4' THEN '여' END AS sex,
       CASE WHEN c2.name = '계' THEN NULL ELSE substring(c2.name FROM '(\\d+)')::int END AS age,
       c2.name LIKE '%%이상' AS is_open_age,
       c2.name AS age_label,
       v.value::bigint AS population
FROM raw.stat_value v
JOIN catalog.stat_table t USING (tbl_key)
JOIN catalog.stat_code i ON i.code_key = v.item_key
JOIN catalog.stat_code c1 ON c1.code_key = v.c1
JOIN catalog.stat_code c2 ON c2.code_key = v.c2
WHERE t.tbl_id = %s AND v.prd_se = 'A' AND i.code IN ('T2', 'T3', 'T4')
"""

SPAN = "SELECT count(*), min(year), max(year) FROM {}"


def rebuild(pg: psycopg.Connection, tbl_id: str) -> bool:
    if tbl_id != TABLE:
        return False
    with pg.transaction():
        pg.execute(BUILD, [TABLE])
        new = pg.execute(SPAN.format("population_new")).fetchone()
        old = pg.execute(SPAN.format("mart.population")).fetchone()
        if new[0] < old[0] or (old[1] is not None and (new[1] > old[1] or new[2] < old[2])):
            logger.warning(
                "mart.population을 바꾸지 않습니다: "
                "새로 만든 %s행(%s~%s년)이 지금(%s행, %s~%s년)보다 적음",
                *new,
                *old,
            )
            return False
        pg.execute("DELETE FROM mart.population")
        pg.execute("INSERT INTO mart.population SELECT * FROM population_new")
    pg.execute("ANALYZE mart.population")
    logger.info(
        "mart.population 다시 만듦: %s행, %s~%s년 (전에는 %s~%s년)", new[0], *new[1:], *old[1:]
    )
    return True
