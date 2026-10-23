"""전체 데이터 파이프라인 한 번에 실행 (Windows / macOS 공통)

  1. 원본 압축 해제   data/kosis_processed.zip → data/raw/kosis_zip/  (이미 풀려 있으면 건너뜀)
  2. preprocess.py    CSV → data/processed/kosis.duckdb
  3. catalog.py       config/catalog_notes.toml → DB
  4. validate.py      정합성 검증 (실패하면 중단)
  옵션
    --preview   export_preview.py: 엑셀로 열어볼 CSV (data/processed/preview/)
    --share     make_share.py: 팀 공유용 zip (dist/), --preview 포함

실행: python pipeline/run_all.py [--preview] [--share]   (가상환경 활성화 후, README 참고)
"""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "pipeline"
RAW_ZIP = ROOT / "data" / "kosis_processed.zip"
RAW_DIR = ROOT / "data" / "raw" / "kosis_zip"


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


def run(script):
    print(f"\n===== {script} =====", flush=True)
    result = subprocess.run([sys.executable, str(SRC / script)], cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(f"\n{script} 실패 (종료 코드 {result.returncode}). 위 출력을 확인하세요.")


def main():
    parser = argparse.ArgumentParser(description="KOSIS 데이터 파이프라인 실행")
    parser.add_argument("--preview", action="store_true", help="엑셀용 미리보기 CSV 만들기")
    parser.add_argument("--share", action="store_true", help="팀 공유용 zip 만들기 (미리보기 포함)")
    args = parser.parse_args()

    extract_raw()
    for script in ["preprocess.py", "catalog.py", "validate.py"]:
        run(script)
    if args.preview or args.share:
        run("export_preview.py")
    if args.share:
        run("make_share.py")
    print("\n모든 단계 완료")


if __name__ == "__main__":
    main()
