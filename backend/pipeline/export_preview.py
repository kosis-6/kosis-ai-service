import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.core.stats_db import rows  # noqa: E402

OUT_DIR = ROOT / "data" / "processed" / "preview"

QUERIES = {
    "catalog": "select * from catalog order by tbl_id",
    "regions": "select * from regions order by region_code",
    "region_aliases": "select * from region_aliases order by region_code, alias",
    "region_changes": "select * from region_changes order by year",
    "region_code_map": "select * from region_code_map order by tbl_id, src_code",
    "facts_sample": """
        select * from (
            select *, row_number() over (
                partition by tbl_id order by year desc, c1_id, c2_id, c3_id
            ) as rn
            from facts where not is_missing
        ) a where rn <= 200
        union all
        select * from (
            select *, row_number() over (partition by tbl_id, missing_reason order by year) as rn
            from facts where is_missing
        ) b where rn <= 20
        order by tbl_id, is_missing, year desc
    """,
    "population_sample": """
        select p.*, r.region_name from population p join regions r using (region_code)
        where (p.region_code = '11' and p.year = 2025)
           or (p.region_code = '00' and p.age is null)
        order by p.region_code, p.year, p.sex, p.age nulls first
    """,
}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, sql in QUERIES.items():
        df = pd.DataFrame(rows(sql)).drop(columns=["rn"], errors="ignore")
        df.to_csv(OUT_DIR / f"{name}.csv", index=False, encoding="utf-8-sig")
        print(f"  {name:18s} {len(df):>7,}행")
    print(f"\n완료: {OUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
