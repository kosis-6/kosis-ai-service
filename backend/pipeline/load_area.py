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
YEARS_PER_REQUEST = 7

SIDO = {
    "서울": "11", "부산": "26", "대구": "27", "인천": "28", "광주": "29", "대전": "30",
    "울산": "31", "세종": "36", "세종특별자치시": "36", "경기": "41", "강원": "51",
    "충북": "43", "충남": "44", "전북": "52", "전남": "46", "경북": "47", "경남": "48",
    "제주": "50",
}  # fmt: skip
TOTAL_NAMES = {"전국", "합계", "계"}
SUFFIX = re.compile(r"\((?:계|통합)\)$")
SAME_AS_SIDO = {"36": "36110"}
OLD_NAMES = {"미추홀구": "남구"}

AREA_DDL = """
    CREATE TABLE IF NOT EXISTS mart.area (
        year        integer NOT NULL,
        region_code text    NOT NULL,
        area_km2    double precision NOT NULL,
        PRIMARY KEY (year, region_code)
    )
"""


def _get(path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    settings = get_settings()
    if not settings.kosis_api_key:
        raise RuntimeError("KOSIS_API_KEY가 없습니다. .env에 넣어 주세요.")
    params = {"apiKey": settings.kosis_api_key, "format": "json", "jsonVD": "Y", **params}
    with httpx.Client(base_url=settings.kosis_base_url, timeout=TIMEOUT) as client:
        response = client.get(path, params=params)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}")
    data = response.json()
    if isinstance(data, dict):
        raise RuntimeError(f"KOSIS {data.get('err')}: {data.get('errMsg')}")
    return data


def _codes() -> tuple[str, str, str, str]:
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
    span = next(r for r in rows if r.get("PRD_SE") in ("Y", "년"))
    return int(span["STRT_PRD_DE"]), int(span["END_PRD_DE"])


def fetch() -> list[dict[str, Any]]:
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


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _in_year(region: dict[str, Any], year: int) -> bool:
    vf, vt = region.get("valid_from"), region.get("valid_to")
    return (vf is None or year >= vf) and (vt is None or year <= vt)


def match(rows: list[dict[str, Any]], regions: list[dict[str, Any]]):
    by_name = defaultdict(list)
    for r in regions:
        if r["kind"] in ("시군구", "일반구"):
            by_name[(r["sido_code"], SUFFIX.sub("", _norm(r["region_name"])))].append(r)
    sido_names = {_norm(k) for k in SIDO}
    out, missed = {}, []
    for row in rows:
        year, sido, name = row["year"], _norm(row["sido"]), SUFFIX.sub("", _norm(row["name"]))
        code = None
        if name in TOTAL_NAMES:
            code = "00" if sido in TOTAL_NAMES else SIDO.get(sido)
        elif name == sido or (name in sido_names and SIDO.get(name) == SIDO.get(sido)):
            code = SIDO.get(sido)
        elif sido_code := SIDO.get(sido):
            found = [r for r in by_name[(sido_code, name)] if _in_year(r, year)]
            if not found and name in OLD_NAMES:
                found = [r for r in by_name[(sido_code, OLD_NAMES[name])] if _in_year(r, year)]
            if not found:
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
        if (year, code) in out:
            continue
        out[(year, code)] = row["area_m2"] / 1_000_000
    for (year, sido_code), km2 in list(out.items()):
        if sido_code in SAME_AS_SIDO:
            out.setdefault((year, SAME_AS_SIDO[sido_code]), km2)
    return out, missed


def _regions(pg: psycopg.Connection) -> list[dict[str, Any]]:
    cur = pg.execute(
        "SELECT region_code, region_name, kind, sido_code, valid_from, valid_to FROM mart.regions"
    )
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def save(pg: psycopg.Connection, area: dict[tuple[int, str], float]) -> None:
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
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        rows = fetch()
    except Exception as e:  # noqa: BLE001 - 어떤 이유든 이 단계만 건너뛴다
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
