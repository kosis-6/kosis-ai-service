import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import zipfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SRC = ROOT / "pipeline"
RAW_ZIP = ROOT / "data" / "kosis_processed.zip"
RAW_DIR = ROOT / "data" / "raw" / "kosis_zip"
SOURCES = {"zip": RAW_DIR / "processed", "collect": ROOT / "data" / "processed"}


def extract_raw(zip_path=RAW_ZIP, dest=RAW_DIR):
    if (dest / "processed").exists():
        return
    if not zip_path.exists():
        raise SystemExit(f"원본 데이터가 없습니다. {zip_path.name}을(를) 프로젝트 폴더에 두세요.")
    print(f"원본 압축 해제: {zip_path.name} → {dest}")
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if not info.flag_bits & 0x800:
                try:
                    name = name.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            target = (dest / name).resolve()
            if not target.is_relative_to(root):
                raise SystemExit(f"압축 파일에 잘못된 경로가 있습니다: {name}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def run(script, env=None):
    print(f"\n===== {script} =====", flush=True)
    result = subprocess.run(
        [sys.executable, str(SRC / script)], cwd=ROOT, env={**os.environ, **(env or {})}
    )
    if result.returncode != 0:
        raise SystemExit(f"\n{script} 실패 (종료 코드 {result.returncode}). 위 출력을 확인하세요.")


def prepare_deferred():
    from pipeline import load_catalog
    from pipeline.catalog import load_notes, missing_deferred
    from pipeline.stats_build import Build

    with closing(Build()) as con:
        needed = missing_deferred(con, load_notes())
        known = set()
        if needed and con.sql("select to_regclass('catalog.table_meta')").fetchall()[0][0]:
            known = {
                row[0]
                for row in con.execute(
                    "select tbl_id from catalog.table_meta where tbl_id = any(?)",
                    [sorted(needed)],
                ).fetchall()
            }
    if missing := needed - known:
        if not load_catalog.CATALOG.is_file():
            raise SystemExit(
                f"공유본에 없는 표를 등록하려면 {load_catalog.CATALOG} 파일이 필요합니다. "
                f"대상 표: {', '.join(sorted(missing))}"
            )
        records = [r for r in load_catalog.read_catalog() if r["tbl_id"] in missing]
        if absent := missing - {r["tbl_id"] for r in records}:
            raise SystemExit(f"카탈로그에 필요한 표가 없습니다: {', '.join(sorted(absent))}")
        load_catalog.load(records, load_catalog.database_url(), prune=False)
    return sorted(needed)


def main():
    parser = argparse.ArgumentParser(description="KOSIS 데이터 파이프라인 실행")
    parser.add_argument(
        "--source", choices=SOURCES, default="zip", help="원본: zip(공유 파일), collect(수집 결과)"
    )
    parser.add_argument("--preview", action="store_true", help="엑셀용 미리보기 CSV 만들기")
    parser.add_argument("--share", action="store_true", help="팀 공유용 zip 만들기 (미리보기 포함)")
    args = parser.parse_args()

    if args.source == "zip":
        extract_raw()
    env = {"KOSIS_RAW_DIR": str(SOURCES[args.source])}
    for script in ("preprocess.py", "catalog.py"):
        run(script, env)
    deferred = prepare_deferred()
    for script in [
        "validate.py",
        "publish_stats.py",
        "load_raw.py",
        "region_map.py",
        "load_area.py",
    ]:
        run(script, env)
    if deferred:
        from app.services import load_queue

        queued = asyncio.run(load_queue.enqueue_missing(deferred, "indicator"))
        if queued:
            print(f"\n공유본 외 통계표 {len(queued)}개를 적재 대기열에 넣었습니다.")
    print("\n검증 통과: 서비스 데이터 교체 (실행 중인 서버는 재시작해야 반영)")
    if args.preview or args.share:
        run("export_preview.py")
    if args.share:
        run("make_share.py")
    print("\n모든 단계 완료")


if __name__ == "__main__":
    main()
