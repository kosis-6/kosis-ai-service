"""전처리 결과(kosis.duckdb)를 엑셀로 열어볼 수 있는 CSV로 내보내기

출력: data/processed/preview/<테이블>.csv  (엑셀 한글 깨짐 방지: utf-8-sig)
  - 작은 표(catalog, regions 등)는 전체
  - 큰 표(facts, population)는 예시 행만 (엑셀 최대 행 수 약 100만 행 초과)

실행: python pipeline/export_preview.py   (가상환경 활성화 후, README 참고)
"""

from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "processed" / "kosis.duckdb"
OUT_DIR = ROOT / "data" / "processed" / "preview"

QUERIES = {
    "catalog": "select * from catalog order by tbl_id",
    "regions": "select * from regions order by region_code",
    "region_aliases": "select * from region_aliases order by region_code, alias",
    "region_changes": "select * from region_changes order by year",
    "region_code_map": "select * from region_code_map order by tbl_id, src_code",
    # 표마다 최근 연도 일부 + 결측 예시
    "facts_sample": """
        select * from (
            select *, row_number() over (
                partition by tbl_id order by year desc, c1_id, c2_id, c3_id
            ) as rn
            from facts where not is_missing
        ) where rn <= 200
        union all
        select * from (
            select *, row_number() over (partition by tbl_id, missing_reason order by year) as rn
            from facts where is_missing
        ) where rn <= 20
        order by tbl_id, is_missing, year desc
    """,
    # 서울특별시 2025년 성별·연령별 전체 + 전국 연도별 합계
    "population_sample": """
        select p.*, r.region_name from population p join regions r using (region_code)
        where (p.region_code = '11' and p.year = 2025)
           or (p.region_code = '00' and p.age is null)
        order by p.region_code, p.year, p.sex, p.age nulls first
    """,
}


def main():
    if not DB_PATH.exists():
        raise SystemExit("먼저 pipeline/run_all.py를 실행하세요.")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH), read_only=True)
    for name, sql in QUERIES.items():
        df = con.sql(sql).df().drop(columns=["rn"], errors="ignore")
        df.to_csv(OUT_DIR / f"{name}.csv", index=False, encoding="utf-8-sig")
        print(f"  {name:18s} {len(df):>7,}행")
    con.close()
    print(f"\n완료: {OUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
