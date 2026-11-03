"""검색 데이터 한 번에 다시 만들기: 카탈로그 → stat_tables → table_families → table_embeddings

실전 2 통계표 검색이 쓰는 PostgreSQL 테이블을 순서대로 다시 만든다.
메타데이터·패밀리 규칙·검색 문서 구성이 바뀌었을 때 이것 하나만 실행하면 된다.

  1. pipeline.load_catalog       카탈로그 파일 → stat_tables (tbl_id 기준 갱신)
  2. pipeline.build_families     stat_tables → table_families (새로 만듦)
  3. pipeline.build_embeddings   stat_tables → table_embeddings (캐시가 있으면 재사용)

서버는 표 정보를 처음 한 번 읽어 두므로 끝나면 서버를 재시작해야 한다.
3단계에서 캐시에 없는 표가 있으면 서버 컨테이너에서는 bge-m3를 계산할 수 없어 멈춘다.
그때는 내 PC에서 캐시를 만든 뒤 --skip-catalog로 다시 실행한다.

실행
  docker compose exec api python -m pipeline.build_search
  docker compose exec api python -m pipeline.build_search --skip-catalog   # 카탈로그는 그대로
  docker compose restart api
"""

import argparse
import sys

from pipeline import build_embeddings, build_families, load_catalog
from pipeline.search_docs import PRESETS


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--skip-catalog", action="store_true", help="1단계(stat_tables)를 건너뜀")
    parser.add_argument("--model", choices=list(build_embeddings.MODELS), default="bge-m3")
    parser.add_argument("--preset", choices=PRESETS, default="full", help="검색 문서 구성")
    args = parser.parse_args()
    url = load_catalog.database_url()

    if not args.skip_catalog:
        print("1/3 stat_tables")
        records = load_catalog.read_catalog()
        errors = load_catalog.check(records)
        if errors:
            print("  완료 기준 미달:\n    " + "\n    ".join(errors), file=sys.stderr)
            return 1
        result = load_catalog.load(records, url)
        print(
            f"  {result['total']}행 (추가 {result['inserted']}, 갱신 {result['updated']},"
            f" 삭제 {result['deleted']})"
        )

    print("2/3 table_families")
    tables = build_families.read_tables(url)
    if not tables:
        print("  stat_tables가 비어 있음. --skip-catalog 없이 실행", file=sys.stderr)
        return 1
    rows = build_families.build(tables)
    print("  " + build_families.report(rows).replace("\n", "\n  "))
    axes = build_families.load(rows, url)
    print(f"  {len(rows)}행 · family_axes {axes}행")

    print("3/3 table_embeddings")
    tables = build_embeddings.read_tables(url)
    docs, vectors, source = build_embeddings.vectors_for(tables, args.model, args.preset)
    n = build_embeddings.load(tables, docs, vectors, args.model, args.preset, url)
    print(f"  {n}행 ({args.model} · {args.preset} · {source})")

    print("\n완료. 서버 재시작: docker compose restart api")
    return 0


if __name__ == "__main__":
    sys.exit(main())
