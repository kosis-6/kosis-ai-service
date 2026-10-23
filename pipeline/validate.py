"""전처리 결과(kosis.duckdb) 정합성 검증

검사마다 '규칙을 어긴 행'을 찾는 SQL을 하나씩 둔다.
  결과가 0행 → 통과(PASS), 1행 이상 → 실패(FAIL) + 예시 출력
실패가 하나라도 있으면 종료 코드 1 (나중에 자동 테스트에 연결할 수 있도록)

지역 코드 연결 검증은 preprocess.py 실행 요약에서 따로 출력한다.
카탈로그 검사는 catalog.py 실행 후에 통과한다 (run_all.py가 순서대로 실행).

실행: python pipeline/validate.py   (가상환경 활성화 후, README 참고)
"""

import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "processed" / "kosis.duckdb"

# 비율·소수 값을 비교할 때 허용 오차 (발표값 반올림 차이)
TOLERANCE = 0.01

# (검사 이름, 규칙을 어긴 행을 찾는 SQL)
CHECKS = [
    # ---- population: 1세별 주민등록인구 -------------------------------------
    (
        "population: 값이 비었거나 음수인 행",
        """
        select * from population where population is null or population < 0
    """,
    ),
    (
        "population: 남 + 여 = 계",
        """
        select t.year, t.region_code, t.age_label, t.population as total,
               m.population + f.population as m_plus_f
        from population t
        join population m on m.year = t.year and m.region_code = t.region_code
                         and m.age_label = t.age_label and m.sex = '남'
        join population f on f.year = t.year and f.region_code = t.region_code
                         and f.age_label = t.age_label and f.sex = '여'
        where t.sex = '계' and t.population <> m.population + f.population
    """,
    ),
    (
        "population: 연령별 합계 = 전체(계)",
        """
        select t.year, t.region_code, t.sex, t.population as total, s.age_sum
        from population t
        join (select year, region_code, sex, sum(population) as age_sum
              from population where age is not null group by all) s using (year, region_code, sex)
        where t.age is null and t.population <> s.age_sum
    """,
    ),
    (
        "population: 시군구(is_basic) 합계 = 시도",
        """
        select p.year, p.region_code, p.sex, p.population as sido, s.basic_sum
        from population p
        join (select p.year, r.sido_code, p.sex, sum(p.population) as basic_sum
              from population p join regions r using (region_code)
              where r.is_basic and p.age is null group by all) s
          on s.year = p.year and s.sido_code = p.region_code and s.sex = p.sex
        where p.age is null and p.population <> s.basic_sum
    """,
    ),
    (
        "population: 시도 합계 = 전국",
        """
        select n.year, n.sex, n.population as nation, s.sido_sum
        from population n
        join (select p.year, p.sex, sum(p.population) as sido_sum
              from population p join regions r using (region_code)
              where r.kind = '시도' and p.age is null group by all) s using (year, sex)
        where n.region_code = '00' and n.age is null and n.population <> s.sido_sum
    """,
    ),
    (
        "population: 연도·지역마다 0~100세 이상이 빠짐없이 있음 (102개 = 계 + 101개 연령)",
        """
        select year, region_code, sex, count(*) as n
        from population group by all having count(*) <> 102
    """,
    ),
    # ---- facts: 통계표 20개 통합 ----------------------------------------------
    (
        "facts: 결측이 아닌데 값이 빈 행",
        """
        select tbl_id, year, label, c1_nm from facts where not is_missing and value is null
    """,
    ),
    (
        "facts: 결측인데 사유가 없는 행",
        """
        select tbl_id, year, label, c1_nm from facts where is_missing and missing_reason is null
    """,
    ),
    (
        "facts: 단위가 없는 행",
        """
        select distinct tbl_id, label from facts where unit is null
    """,
    ),
    (
        "facts: 수록 기간(catalog) 밖 연도",
        """
        select f.tbl_id, min(f.year) as min_year, max(f.year) as max_year, c.start_year, c.end_year
        from facts f join catalog c using (tbl_id)
        group by f.tbl_id, c.start_year, c.end_year
        having min(f.year) < c.start_year or max(f.year) > c.end_year
    """,
    ),
    (
        "facts: 추계표가 아닌데 추계(is_projection)로 표시된 행",
        """
        select distinct f.tbl_id from facts f join catalog c using (tbl_id)
        where f.is_projection and not c.is_projection_table
    """,
    ),
    (
        "facts: 추계표인데 시나리오가 빈 행",
        """
        select distinct f.tbl_id, f.c1_nm from facts f join catalog c using (tbl_id)
        where c.is_projection_table and f.scenario is null
    """,
    ),
    # ---- 카탈로그 (catalog.py 결과) -------------------------------------------
    (
        "카탈로그: 설명(table_notes)이 없는 표",
        """
        select c.tbl_id from catalog c left join table_notes n using (tbl_id) where n.tbl_id is null
    """,
    ),
    (
        "카탈로그: 지표 사전이 가리키는 표가 데이터에 없음",
        """
        select s.indicator, s.tbl_id from indicator_sources s left join catalog c using (tbl_id)
        where c.tbl_id is null
    """,
    ),
    (
        "카탈로그: 지표 사전의 조회 항목이 facts에 없음",
        """
        select s.indicator, s.tbl_id, s.item_ids
        from indicator_sources s
        where s.item_ids is not null and not exists (
            select 1 from facts f
            where f.tbl_id = s.tbl_id and list_contains(string_split(s.item_ids, ','), f.item_id))
    """,
    ),
]

# ---- 표끼리 교차 검증: 같은 지표를 다른 표에서 비교 ------------------------------
# 지역 코드가 표마다 올바르게 연결됐는지도 함께 확인된다.
# 비교한 쌍이 MIN_PAIRS보다 적으면(연결이 끊기면 0쌍이 되어 '위반 0건'으로 통과해 버림) 실패로 본다.
MIN_PAIRS = 100
# (검사 이름, 두 표를 연결하는 조건)
CROSS_CHECKS = [
    (
        "교차: 출생아 수 (인구동태 DT_1B8000H = 시군구 출생 DT_1B81A23)",
        """
        a.tbl_id = 'DT_1B8000H' and a.item_id = 'T10'
        and b.tbl_id = 'DT_1B81A23' and b.item_id = 'T1'
    """,
    ),
    (
        "교차: 출생아 수 (인구동태 DT_1B8000H = 모의 연령별 출생 DT_1B81A12 총계)",
        """
        a.tbl_id = 'DT_1B8000H' and a.item_id = 'T10'
        and b.tbl_id = 'DT_1B81A12' and b.item_id = 'T1' and b.c2_id = '00' and b.c3_id = '00'
    """,
    ),
    (
        "교차: 합계출산율 (인구동태 DT_1B8000H = 출산율 DT_1B81A21)",
        """
        a.tbl_id = 'DT_1B8000H' and a.item_id = 'T12'
        and b.tbl_id = 'DT_1B81A21' and b.item_id = 'T1'
    """,
    ),
]


def check_indicator_names(con):
    """indicators.py의 계산 지표와 카탈로그 지표 사전(computed)의 이름이 같은지"""
    # 서비스 코드(app/services/indicators.py)를 불러오려면 프로젝트 루트가 경로에 있어야 함
    sys.path.insert(0, str(ROOT))
    from app.services.indicators import INDICATORS

    in_catalog = {
        r[0]
        for r in con.sql("select name from indicator_dict where method = 'computed'").fetchall()
    }
    in_code = set(INDICATORS)
    return [{"only_in": "카탈로그", "name": n} for n in sorted(in_catalog - in_code)] + [
        {"only_in": "indicators.py", "name": n} for n in sorted(in_code - in_catalog)
    ]


def main():
    if not DB_PATH.exists():
        raise SystemExit("먼저 pipeline/run_all.py를 실행하세요.")
    con = duckdb.connect(str(DB_PATH), read_only=True)

    results = []  # (이름, 위반 행 DataFrame 또는 list, 추가 설명)

    for name, sql in CHECKS:
        try:
            results.append((name, con.sql(sql).df(), ""))
        except duckdb.CatalogException as e:  # catalog.py를 안 돌려 테이블이 없는 경우
            results.append(
                (name, [{"error": str(e).splitlines()[0]}], "catalog.py를 먼저 실행하세요.")
            )

    for name, cond in CROSS_CHECKS:
        base = f"from facts a join facts b using (year, region_code) where {cond}"
        pairs = con.sql(f"select count(*) {base}").fetchall()[0][0]
        bad = con.sql(f"""select a.year, a.region_code, a.value as a_value, b.value as b_value
                          {base} and abs(a.value - b.value) > {TOLERANCE}""").df()
        if pairs < MIN_PAIRS:
            results.append(
                (
                    name,
                    [{"pairs": pairs, "min_pairs": MIN_PAIRS}],
                    "비교한 쌍이 너무 적음 (지역·연도 연결 확인)",
                )
            )
        else:
            results.append((f"{name} [{pairs}쌍 비교]", bad, ""))

    try:
        results.append(
            (
                "카탈로그: 계산 지표 이름 일치 (indicators.py ↔ 지표 사전)",
                check_indicator_names(con),
                "",
            )
        )
    except duckdb.CatalogException as e:
        results.append(
            (
                "카탈로그: 계산 지표 이름 일치",
                [{"error": str(e).splitlines()[0]}],
                "catalog.py를 먼저 실행하세요.",
            )
        )
    con.close()

    failed = 0
    for name, bad, hint in results:
        n = len(bad)
        if n == 0:
            print(f"[PASS] {name}")
        else:
            failed += 1
            print(f"[FAIL] {name}  → {n:,}건 {hint}")
            print(pd.DataFrame(bad).head(5).to_string(index=False))
            print()

    print(f"\n총 {len(results)}개 검사 중 통과 {len(results) - failed}개, 실패 {failed}개")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
