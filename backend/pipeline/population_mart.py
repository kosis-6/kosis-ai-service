"""갱신 뒤 mart.population(1세별 주민등록인구)을 raw.stat_value에서 다시 만든다

mart.population은 전처리(preprocess.build_population)가 CSV에서 만든다. 매일 밤 갱신은
raw.stat_value에만 새 시점을 덧붙이므로(load_raw.merge_into) 그대로 두면 계산 지표
(총인구·고령인구 비율 등)가 새 연간 값을 보지 못한다. 그래서 DT_1B04006을 적재한 뒤
같은 규칙으로 raw.stat_value에서 다시 만든다 (연 단위 시점만. 월별 값은 population_latest가 쓴다).

규칙은 preprocess.build_population과 같다 (2026-11-14에 두 결과가 156만 행 모두 같음을 확인).
새로 만든 행이 지금보다 적거나 연도가 줄면 바꾸지 않는다 (기존 값을 지우지 않는다).

load_worker의 적재 후 단계(AFTER_LOAD)로 실행된다. 다른 표면 아무것도 하지 않는다.
"""

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
    """tbl_id가 주민등록인구 표면 mart.population을 다시 만든다. 바꿨으면 True"""
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
