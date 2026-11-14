"""행정구역 면적 적재 (인구밀도 계산용) → mart.area

인구밀도 = 주민등록인구 ÷ 면적(㎢). 인구는 mart.population, 면적은 이 파일이 넣는 mart.area를 쓴다
(app/services/indicators.py의 "인구밀도").

  출처  국토교통부 지적통계, 행정구역별·지목별 국토이용현황_시군구 (KOSIS DT_MLTM_2300)
        지목 '계'의 면적. 각 연도 12월 31일 기준 (주민등록인구와 같은 기준일)
  단위  표는 ㎡ → ㎢로 바꿔 넣는다

이 표는 지역이 표준 코드가 아니라 이름(시도 약칭 '서울' + 시군구 이름 '종로구')으로 되어 있다.
그해 있던 지역(mart.regions의 valid_from·valid_to)에서 같은 시도·같은 이름을 찾아 코드를 붙인다.
  - 시도 칸과 시군구 칸이 같은 이름이면 시도 합계 행, '전국'이면 전국
  - '포항시(계)'·'창원시(통합)' 같은 꼬리는 떼고, '포항시남구'는 일반구 '남구'로 찾는다
  - 같은 시도에 같은 이름이 둘 이상이면 (옛 부천 원미구·새 원미구 등) 그해 있던 것만 남긴다
  찾지 못한 이름은 건너뛰고 끝에 목록을 보여준다

run_all.py가 마지막에 실행한다. publish_stats.py가 바꾸는 표 목록에 없어 다른 단계가 지우지 않는다.

실행 (backend/ 에서, .env의 KOSIS_API_KEY 필요. 없거나 받지 못하면 건너뛰고 기존 값 유지)
  python -m pipeline.load_area            전체 연도를 받아 mart.area를 바꾼다
  python -m pipeline.load_area --dry-run  받기와 지역 맞추기만 (DB에 쓰지 않음)
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402
from pipeline.stats_build import conninfo  # noqa: E402

logger = logging.getLogger("load_area")

ORG_ID = "116"
TABLE = "DT_MLTM_2300"
SOURCE = "국토교통부 지적통계, 행정구역별·지목별 국토이용현황_시군구 (DT_MLTM_2300)"
TIMEOUT = 120
# 시도 19 × 시군구 277 = 한 해 5,263칸. KOSIS 한도(4만 칸) 안에 들도록 7년씩 받는다
YEARS_PER_REQUEST = 7

# 표의 시도 약칭 → 지역 코드 (mart.regions, 행정표준코드)
SIDO = {
    "서울": "11", "부산": "26", "대구": "27", "인천": "28", "광주": "29", "대전": "30",
    "울산": "31", "세종": "36", "세종특별자치시": "36", "경기": "41", "강원": "51",
    "충북": "43", "충남": "44", "전북": "52", "전남": "46", "경북": "47", "경남": "48",
    "제주": "50",
}  # fmt: skip
TOTAL_NAMES = {"전국", "합계", "계"}
SUFFIX = re.compile(r"\((?:계|통합)\)$")
# 시군구가 시도 하나뿐인 곳: 시도 코드 → 시군구 코드 (면적이 같다)
SAME_AS_SIDO = {"36": "36110"}
# 표가 바뀐 뒤 이름을 그 전 해에 쓴 경우 (2012년 인천 '미추홀구' = 당시 남구)
OLD_NAMES = {"미추홀구": "남구"}

AREA_DDL = """
    CREATE TABLE IF NOT EXISTS mart.area (
        year        integer NOT NULL,
        region_code text    NOT NULL,
        area_km2    double precision NOT NULL,
        PRIMARY KEY (year, region_code)
    )
"""


# ---- KOSIS ----------------------------------------------------------------------


def _get(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.kosis_api_key:
        raise RuntimeError("KOSIS_API_KEY가 없습니다. .env에 넣어 주세요.")
    params = {"apiKey": settings.kosis_api_key, "format": "json", "jsonVD": "Y", **params}
    with httpx.Client(base_url=settings.kosis_base_url, timeout=TIMEOUT) as client:
        response = client.get(path, params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")  # 주소(apiKey 포함)는 남기지 않는다
    data = response.json()
    if isinstance(data, dict):  # {"err": "...", "errMsg": "..."}
        raise RuntimeError(f"KOSIS {data.get('err')}: {data.get('errMsg')}")
    return data


def _codes() -> tuple[str, str, str, str]:
    """표 메타에서 면적 항목 코드, 지목 '계' 코드, 시도·시군구 분류 ID"""
    meta = _get(
        "/statisticsData.do", {"method": "getMeta", "type": "ITM", "orgId": ORG_ID, "tblId": TABLE}
    )
    item = next(m["ITM_ID"] for m in meta if m["OBJ_ID"] == "ITEM" and m["ITM_NM"] == "면적")
    objs = {m["OBJ_NM"]: m["OBJ_ID"] for m in meta if m["OBJ_ID"] != "ITEM"}
    land = objs["레벨01"]
    total = next(m["ITM_ID"] for m in meta if m["OBJ_ID"] == land and m["ITM_NM"] == "계")
    return item, total, objs["시도"], objs["시군구"]


def _years() -> tuple[int, int]:
    rows = _get(
        "/statisticsData.do", {"method": "getMeta", "type": "PRD", "orgId": ORG_ID, "tblId": TABLE}
    )
    # [{PRD_SE, STRT_PRD_DE, END_PRD_DE}]
    span = next(r for r in rows if r.get("PRD_SE") in ("Y", "년"))
    return int(span["STRT_PRD_DE"]), int(span["END_PRD_DE"])


def fetch() -> list[dict[str, Any]]:
    """전체 연도의 면적 행 [{year, sido, name, area_m2}]"""
    item, total, _, _ = _codes()
    lo, hi = _years()
    out = []
    for y0 in range(lo, hi + 1, YEARS_PER_REQUEST):
        y1 = min(y0 + YEARS_PER_REQUEST - 1, hi)
        rows = _get(
            "/Param/statisticsParameterData.do",
            {
                "method": "getList", "orgId": ORG_ID, "tblId": TABLE, "itmId": item,
                "objL1": "ALL", "objL2": "ALL", "objL3": total,
                "prdSe": "Y", "startPrdDe": str(y0), "endPrdDe": str(y1),
            },
        )  # fmt: skip
        for r in rows:
            if r.get("DT") in (None, "", "-"):
                continue
            out.append(
                {
                    "year": int(r["PRD_DE"]),
                    "sido": r["C1_NM"].strip(),
                    "name": r["C2_NM"].strip(),
                    "area_m2": float(r["DT"]),
                }
            )
        logger.info("%d~%d년 %d행", y0, y1, len(rows))
    return out


# ---- 지역 맞추기 -------------------------------------------------------------------


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _in_year(region: dict[str, Any], year: int) -> bool:
    vf, vt = region.get("valid_from"), region.get("valid_to")
    return (vf is None or year >= vf) and (vt is None or year <= vt)


def match(rows: list[dict[str, Any]], regions: list[dict[str, Any]]):
    """면적 행 → ({(연도, 지역 코드): ㎢}, 못 찾은 (연도, 시도, 이름) 목록)

    regions: mart.regions 행 (region_code, region_name, kind, sido_code, valid_from, valid_to)"""
    by_name = defaultdict(list)  # (시도 코드, 이름) → 지역들
    for r in regions:
        if r["kind"] in ("시군구", "일반구"):
            by_name[(r["sido_code"], SUFFIX.sub("", _norm(r["region_name"])))].append(r)
    sido_names = {_norm(k) for k in SIDO}
    out, missed = {}, []
    for row in rows:
        year, sido, name = row["year"], _norm(row["sido"]), SUFFIX.sub("", _norm(row["name"]))
        code = None
        # '전국 합계' → 전국, '울산 합계' → 울산 (어떤 해는 시도 행 이름이 합계)
        if name in TOTAL_NAMES:
            code = "00" if sido in TOTAL_NAMES else SIDO.get(sido)
        elif name == sido or (name in sido_names and SIDO.get(name) == SIDO.get(sido)):
            code = SIDO.get(sido)
        elif sido_code := SIDO.get(sido):
            found = [r for r in by_name[(sido_code, name)] if _in_year(r, year)]
            if not found and name in OLD_NAMES:
                found = [r for r in by_name[(sido_code, OLD_NAMES[name])] if _in_year(r, year)]
            if not found:  # '포항시남구' → 일반구 '남구' (앞의 시 이름을 뗀다)
                if m := re.match(r"(.+?시)(.+구)$", name):
                    found = [
                        r
                        for r in by_name[(sido_code, m.group(2))]
                        if r["kind"] == "일반구" and _in_year(r, year)
                    ]
            if len(found) == 1:
                code = found[0]["region_code"]
        if code is None:
            missed.append((year, row["sido"], row["name"]))
            continue
        if (year, code) in out:  # 같은 지역이 두 번 (시도 행과 '합계' 행, 세종의 두 시도 칸)
            continue
        out[(year, code)] = row["area_m2"] / 1_000_000
    for (year, sido_code), km2 in list(out.items()):  # 세종: 시도이면서 시군구 하나
        if sido_code in SAME_AS_SIDO:
            out.setdefault((year, SAME_AS_SIDO[sido_code]), km2)
    return out, missed


# ---- DB ----------------------------------------------------------------------------


def _regions(pg: psycopg.Connection) -> list[dict[str, Any]]:
    cur = pg.execute(
        "SELECT region_code, region_name, kind, sido_code, valid_from, valid_to FROM mart.regions"
    )
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def save(pg: psycopg.Connection, area: dict[tuple[int, str], float]) -> None:
    """mart.area를 새 값으로 바꾼다 (한 트랜잭션: 실패하면 기존 값이 남는다)"""
    with pg.transaction():
        pg.execute(AREA_DDL)
        pg.execute("DELETE FROM mart.area")
        with pg.cursor() as cur:
            cur.executemany(
                "INSERT INTO mart.area (year, region_code, area_km2) VALUES (%s, %s, %s)",
                [(y, code, km2) for (y, code), km2 in sorted(area.items())],
            )
        pg.execute("ANALYZE mart.area")


def main() -> None:
    parser = argparse.ArgumentParser(description="행정구역 면적 적재 (mart.area)")
    parser.add_argument("--dry-run", action="store_true", help="DB에 쓰지 않고 맞추기 결과만")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 요청 주소(apiKey 포함)를 찍지 않는다

    # run_all.py의 마지막 단계: 받지 못하거나 응답 모양이 달라도(KOSIS가 분류 이름을 바꿈,
    # JSON이 아닌 오류 화면 등) 앞 단계 결과와 기존 mart.area는 그대로 두고 건너뛴다
    try:
        rows = fetch()
    except Exception as e:  # noqa: BLE001 - 어떤 이유든 이 단계만 건너뛴다
        # httpx 오류 문구에는 요청 주소(apiKey 포함)가 들어갈 수 있어 종류만 남긴다
        why = str(e) if isinstance(e, RuntimeError) else type(e).__name__
        print(f"면적 자료를 받지 못해 건너뜁니다 (기존 mart.area 유지): {why}")
        return
    with psycopg.connect(conninfo()) as pg:
        area, missed = match(rows, _regions(pg))
        if not area:
            print("면적 자료에서 맞춘 지역이 없어 건너뜁니다 (기존 mart.area 유지)")
            return
        years = sorted({y for y, _ in area})
        print(f"{SOURCE}: {years[0]}~{years[-1]}년, {len(area):,}개 (연도·지역)")
        if missed:
            print(f"지역을 찾지 못한 이름 {len(missed)}개 (건너뜀):")
            for y, s, n in missed[:50]:
                print(f"  {y} {s} {n}")
        if not args.dry_run:
            save(pg, area)
            print("mart.area 교체 완료")


if __name__ == "__main__":
    main()
