"""팀 공유용 전처리 자료 묶기 → dist/kosis_share_YYYYMMDD.zip

담는 것
  preview/*.csv          엑셀로 열어볼 미리보기
  catalog_notes.toml     통계표 설명서 (DB에 들어간 설명의 원본)
  DATA_README.md         자료 설명 (이 스크립트가 DB 내용을 보고 생성)

API 키(.env)나 원본 CSV는 담지 않는다.
실행: python pipeline/run_all.py --share   (검증까지 통과한 뒤 묶기)
"""

import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.stats_db import rows as db_rows  # noqa: E402

PREVIEW_DIR = ROOT / "data" / "processed" / "preview"
NOTES_PATH = ROOT / "config" / "catalog_notes.toml"
DIST_DIR = ROOT / "dist"

TABLE_DESCRIPTIONS = {
    "population": "1세별 주민등록인구 (연도 × 지역 × 성별 × 나이). age가 NULL이면 전체 합계",
    "facts": "통계표 20개를 합친 세로형 테이블. region_code로 표끼리 연결",
    "regions": "지역 코드 (전국/시도/시군구/일반구/출장소, 유효 기간). 시군구 합산은 is_basic만",
    "region_aliases": "지역 별칭 → 지역 코드 (서울 → 11 등)",
    "region_changes": "행정구역 변경 이력",
    "region_code_map": "표마다 다른 원본 지역 코드 → 기준 코드 대응표",
    "catalog": "통계표 목록 (수록 기간, 추계표 여부)",
    "table_notes": "통계표 설명 (조사명, 기준 시점, 대상, 확인 여부)",
    "table_groups": "비교 그룹 (같은 그룹끼리만 직접 비교)",
    "compatible_groups": "그룹이 달라도 비교 가능한 조합",
    "series_breaks": "통계 기준이 바뀐 해",
    "region_notes": "특정 지역에만 해당하는 주의사항",
    "indicator_dict": "지표 사전 (이름, 단위, 별칭)",
    "indicator_sources": "지표 → 표·항목·지역 수준",
}


def build_readme():
    counts = {t: db_rows(f"select count(*) as n from {t}")[0]["n"] for t in TABLE_DESCRIPTIONS}
    span = db_rows("select min(year) as lo, max(year) as hi from population")[0]
    lo, hi = span["lo"], span["hi"]
    notes = db_rows("select count(*) as n, count(*) filter (where verified) as v from table_notes")
    n_tables, n_verified = notes[0]["n"], notes[0]["v"]
    rows = "\n".join(f"| `{t}` | {counts[t]:,} | {d} |" for t, d in TABLE_DESCRIPTIONS.items())
    return f"""# KOSIS 인구통계 전처리 자료

만든 날짜: {date.today().isoformat()}
주민등록인구 수록 기간: {lo}~{hi}년 (연간, 12월 말 기준)
통계표 설명 확인: {n_verified}/{n_tables}개

## 파일

| 파일 | 내용 |
|---|---|
| `preview/*.csv` | 엑셀로 열어볼 미리보기 (큰 표는 일부만) |
| `catalog_notes.toml` | 통계표 설명서 원본 |

## 데이터를 DB에 넣는 방법

원본 zip을 `data/kosis_processed.zip`에 두고
컨테이너 안에서 `python pipeline/run_all.py`를 실행합니다.

지표 계산 함수 (`backend/`에서):

```python
from app.services import indicators
indicators.compare("고령인구 비율", ["서울", "부산"], 2025)
```

## 테이블

| 테이블 | 행 수 | 내용 |
|---|---|---|
{rows}

## 꼭 지킬 규칙

1. 표끼리는 `region_code + year`로만 연결합니다. 지역 이름으로 연결하지 않습니다.
2. 시군구를 더해 시도 값을 만들 때는 `regions.is_basic = true`만 더합니다 (일반구·출장소는 중복).
3. `facts.is_missing = true`인 값은 0이 아닙니다. `missing_reason`으로 이유를 안내합니다.
4. `facts.is_projection = true`는 장래추계값입니다. 실제 통계처럼 답하지 않습니다.
5. 서로 다른 비교 그룹(`table_notes.group_id`)의 값은 직접 비교하지 않습니다.
"""


def main():
    DIST_DIR.mkdir(exist_ok=True)
    out = DIST_DIR / f"kosis_share_{date.today():%Y%m%d}.zip"

    readme = build_readme()

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.write(NOTES_PATH, "catalog_notes.toml")
        z.writestr("DATA_README.md", readme)
        for p in sorted(PREVIEW_DIR.glob("*.csv")):
            z.write(p, f"preview/{p.name}")

    print(f"완료: {out.relative_to(ROOT)} ({out.stat().st_size / 1024 / 1024:.1f}MB)")


if __name__ == "__main__":
    main()
