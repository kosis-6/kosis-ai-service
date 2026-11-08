"""수집이 끝난 표만 골라 raw.stat_value에 적재한다 (pipeline/load_raw.py의 load_table을 그대로 씀).

load_raw.py는 data/processed의 CSV를 처음부터 끝까지 넣고, 한 표에서 오류가 나면 전체가 멈춘다.
수집(collect_all)이 아직 도는 중에 끝난 표부터 넣기 위해 다음만 다르게 한다.

  - {tbl_id}.meta.json이 있는 표(수집 완료)만 넣는다. 받는 중이거나 합치는 중인 표는 건너뛴다
  - 이미 같은 행 수로 적재된 표는 건너뛴다 (다시 실행하면 새로 끝난 표만 넣음)
  - 한 표가 실패해도 기록하고 다음 표로 넘어간다 → data/processed/_work/load_failures.csv
  - 작은 표부터 넣는다

표 하나를 넣는 방법(검증 포함)은 load_raw.load_table과 같다: 중복 칸, 행 수, 값 합계를 확인하고
표 단위 트랜잭션으로 교체한다.

실행 (backend/ 에서, 수집과 다른 창에서 동시에 돌려도 됨)
  python -m scripts.load_done
  python -m scripts.load_done --dry-run    # 넣을 표 목록만
"""

import argparse
import csv
import json
import time
import traceback
from datetime import datetime
from pathlib import Path

import psycopg

from pipeline.load_raw import CSV_DIR, load_table
from pipeline.stats_build import conninfo

FAILURES = CSV_DIR / "_work" / "load_failures.csv"


def finished_tables(csv_dir: Path) -> list[tuple[str, int, Path]]:
    """수집이 끝난 표: (tbl_id, 수집 행 수, CSV 경로). 작은 표부터"""
    done = []
    for meta_path in csv_dir.glob("*.meta.json"):
        tbl_id = meta_path.name.removesuffix(".meta.json")
        path = csv_dir / f"{tbl_id}.csv"
        if not path.exists():
            continue
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        done.append((tbl_id, int(meta.get("rows", -1)), path))
    return sorted(done, key=lambda d: (d[1], d[0]))


def loaded_counts(pg: psycopg.Connection) -> dict[str, int]:
    rows = pg.execute(
        "SELECT t.tbl_id, l.row_count"
        " FROM raw.stat_load l JOIN catalog.stat_table t USING (tbl_key)"
    ).fetchall()
    return dict(rows)


def record_failure(tbl_id: str, error: BaseException) -> None:
    FAILURES.parent.mkdir(parents=True, exist_ok=True)
    new = not FAILURES.exists()
    with FAILURES.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        writer = csv.writer(f)
        if new:
            writer.writerow(["tbl_id", "error", "at"])
        reason = f"{type(error).__name__}: {error}".replace("\n", " ")[:500]
        writer.writerow([tbl_id, reason, datetime.now().isoformat(timespec="seconds")])


def main() -> None:
    parser = argparse.ArgumentParser(description="수집이 끝난 표만 raw.stat_value에 적재")
    parser.add_argument("--dry-run", action="store_true", help="넣을 표 목록만 출력")
    args = parser.parse_args()

    tables = finished_tables(CSV_DIR)
    with psycopg.connect(conninfo(), autocommit=True) as pg:
        if pg.execute("SELECT to_regclass('raw.stat_value')").fetchone()[0] is None:
            raise SystemExit("raw.stat_value가 없습니다. 먼저 alembic upgrade head를 실행하세요.")
        loaded = loaded_counts(pg)
        todo = [t for t in tables if loaded.get(t[0]) != t[1]]
        total_rows = sum(t[1] for t in todo)
        print(f"수집 완료 {len(tables):,}개 / 이미 적재 {len(tables) - len(todo):,}개"
              f" / 이번에 적재 {len(todo):,}개 ({total_rows:,}행)", flush=True)
        if args.dry_run:
            return
        began = time.perf_counter()
        ok = failed = 0
        for i, (tbl_id, _, path) in enumerate(todo, start=1):
            print(f"[{i}/{len(todo)}] ", end="", flush=True)
            try:
                load_table(pg, path)
                ok += 1
            except Exception as e:  # noqa: BLE001 - 한 표 실패로 전체를 멈추지 않는다
                failed += 1
                print(f"{tbl_id} 적재 실패: {type(e).__name__}: {e}", flush=True)
                traceback.print_exc(limit=1)
                record_failure(tbl_id, e)
        minutes = (time.perf_counter() - began) / 60
        print(f"적재 {ok:,}개 / 실패 {failed:,}개 ({minutes:.1f}분)"
              + (f" → 실패 목록: {FAILURES}" if failed else ""), flush=True)


if __name__ == "__main__":
    main()
