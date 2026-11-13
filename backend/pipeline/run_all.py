"""전체 데이터 파이프라인 한 번에 실행 (Windows / macOS 공통)

  1. 원본 준비
       --source zip (기본)  data/kosis_processed.zip → data/raw/kosis_zip/  (풀려 있으면 건너뜀)
       --source collect     scripts/collect_kosis.py 수집 결과(data/processed/*.csv)를 그대로 사용
  2. preprocess.py    CSV → PostgreSQL build 스키마 (서비스가 읽지 않음)
  3. catalog.py       config/catalog_notes.toml → build 스키마
  4. validate.py      정합성 검증
  5. publish_stats.py 검증을 통과한 표를 서비스 스키마(raw, mart, catalog)로 한 번에 옮긴다.
                      중간에 실패하면 서비스 데이터는 그대로 둔다.
  6. load_raw.py      같은 원본 CSV를 숫자 키 구조(raw.stat_value)로 적재. 조회 지표가 읽는다.
                      표마다 대조 후 교체하므로 실패해도 그 표의 기존 값은 남는다.
  7. region_map.py    5에서 region_code_map이 기본 표 것으로 바뀌므로, 그 밖에 적재해 둔 표
                      (적재 프로세스·질문 중 적재)의 지역 대응을 다시 만든다.
  8. load_area.py     인구밀도용 행정구역 면적(지적통계)을 KOSIS에서 받아 mart.area에 넣는다.
                      KOSIS_API_KEY가 없거나 받지 못하면 건너뛴다 (기존 값 유지).
  옵션
    --preview   export_preview.py: 엑셀로 열어볼 CSV (data/processed/preview/)
    --share     make_share.py: 팀 공유용 zip (dist/), --preview 포함

실행: python pipeline/run_all.py [--source collect] [--preview] [--share]   (README 참고)
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "pipeline"
RAW_ZIP = ROOT / "data" / "kosis_processed.zip"
RAW_DIR = ROOT / "data" / "raw" / "kosis_zip"
# 전처리 입력 CSV 폴더. collect는 수집기 출력 폴더 (zip 안의 CSV와 같은 파일)
SOURCES = {"zip": RAW_DIR / "processed", "collect": ROOT / "data" / "processed"}


def extract_raw(zip_path=RAW_ZIP, dest=RAW_DIR):
    """원본 zip 압축 해제. 이 zip은 한글 파일 이름에 UTF-8 표시가 없어 이름을 복원한다."""
    if (dest / "processed").exists():
        return
    if not zip_path.exists():
        raise SystemExit(f"원본 데이터가 없습니다. {zip_path.name}을(를) 프로젝트 폴더에 두세요.")
    print(f"원본 압축 해제: {zip_path.name} → {dest}")
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if not info.flag_bits & 0x800:  # UTF-8 표시 없음 → 파이썬이 cp437로 읽은 이름을 되돌림
                try:
                    name = name.encode("cp437").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            target = (dest / name).resolve()
            if not target.is_relative_to(root):  # zip 안의 '../' 경로로 폴더 밖에 쓰는 것 방지
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
    # 서비스가 읽는 표를 건드리지 않도록 build 스키마에 만들고 검증한 뒤 옮긴다
    env = {"KOSIS_RAW_DIR": str(SOURCES[args.source])}
    for script in [
        "preprocess.py",
        "catalog.py",
        "validate.py",
        "publish_stats.py",
        "load_raw.py",
        "region_map.py",
        "load_area.py",
    ]:
        run(script, env)
    print("\n검증 통과: 서비스 데이터 교체 (실행 중인 서버는 재시작해야 반영)")
    if args.preview or args.share:
        run("export_preview.py")
    if args.share:
        run("make_share.py")
    print("\n모든 단계 완료")


if __name__ == "__main__":
    main()
