import argparse
import asyncio
import csv
import json
from datetime import datetime
from math import prod
from pathlib import Path
from typing import Any

import httpx

from app.services.kosis_client import KosisClient

MAX_CELLS = 40_000  # KOSIS 1회 요청 최대 셀 수
REQUEST_INTERVAL = 1.0  # 분당 호출 제한 대응 (초)
RAW_DIR = Path("data/raw")
OUT_DIR = Path("data/processed")

# 메타데이터의 수록주기 이름 → 요청 파라미터 prdSe 값, 1년당 시점 수
PERIOD_TYPES = {"년": ("Y", 1), "반년": ("H", 2), "분기": ("Q", 4), "월": ("M", 12)}


class KosisError(RuntimeError):
    pass


async def call(client: KosisClient, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    for wait in (5, 15, 30, None):
        try:
            data = await client.get(path, params)
            break
        except httpx.HTTPError:
            # 일시적인 네트워크 오류는 잠시 쉬고 다시 시도
            if wait is None:
                raise
            await asyncio.sleep(wait)
    await asyncio.sleep(REQUEST_INTERVAL)
    # 오류는 {"err": "코드", "errMsg": "..."} 형태로 옴
    if isinstance(data, dict):
        # 30 = 데이터 없음 (예: 폐지된 행정구역만 묶인 요청) → 빈 결과로 처리
        if data.get("err") == "30":
            return []
        raise KosisError(f"{data.get('err')}: {data.get('errMsg')} ({params})")
    return data


async def fetch_meta(client: KosisClient, org_id: str, tbl_id: str) -> dict[str, Any]:
    base = {"method": "getMeta", "orgId": org_id, "tblId": tbl_id}
    tbl = await call(client, "/statisticsData.do", {**base, "type": "TBL"})
    itm = await call(client, "/statisticsData.do", {**base, "type": "ITM"})
    prd = await call(client, "/statisticsData.do", {**base, "type": "PRD"})

    items = [x for x in itm if x["OBJ_ID"] == "ITEM"]
    # 분류는 OBJ_ID_SN 순서가 요청 파라미터 objL1, objL2 ... 순서
    classes: dict[str, dict[str, Any]] = {}
    for x in itm:
        if x["OBJ_ID"] == "ITEM":
            continue
        c = classes.setdefault(
            x["OBJ_ID"], {"sn": int(x["OBJ_ID_SN"]), "name": x["OBJ_NM"], "values": []}
        )
        c["values"].append(x)
    return {
        "tbl_nm": tbl[0]["TBL_NM"],
        "items": items,
        "classes": sorted(classes.values(), key=lambda c: c["sn"]),
        "periods": prd,
    }


def pick_period(periods: list[dict[str, Any]], prd_se: str) -> tuple[str, str, int]:
    """요청할 수록주기의 시작·끝 시점과 예상 시점 수를 고른다."""
    for p in periods:
        code, per_year = PERIOD_TYPES.get(p["PRD_SE"], (None, 0))
        if code == prd_se:
            start, end = p["STRT_PRD_DE"], p["END_PRD_DE"]
            years = int(end[:4]) - int(start[:4]) + 1
            return start, end, years * per_year
    available = [p["PRD_SE"] for p in periods]
    raise KosisError(f"수록주기 {prd_se}가 없습니다. 제공 주기: {available}")


def chunks(values: list[str], size: int) -> list[str]:
    """여러 코드를 +로 이어 한 번에 요청 (예: T1+T2)"""
    return ["+".join(values[i : i + size]) for i in range(0, len(values), size)]


def plan_requests(meta: dict[str, Any], n_periods: int) -> list[dict[str, str]]:
    """셀 수 제한을 넘지 않게 항목을 묶어서, 그래도 크면 값이 가장 많은 분류를 나눠서 요청한다."""
    sizes = [len(c["values"]) for c in meta["classes"]]
    all_classes = {f"objL{i + 1}": "ALL" for i in range(len(sizes))}
    item_ids = [x["ITM_ID"] for x in meta["items"]]
    cells_per_item = prod(sizes) * n_periods
    if cells_per_item <= MAX_CELLS:
        return [
            {"itmId": ids, **all_classes} for ids in chunks(item_ids, MAX_CELLS // cells_per_item)
        ]

    k = sizes.index(max(sizes))
    cells_per_value = cells_per_item // sizes[k]
    if cells_per_value > MAX_CELLS:
        raise KosisError("분류 하나로 나눠도 셀 수 제한을 넘습니다. 기간을 나눠야 합니다.")
    split_values = [v["ITM_ID"] for v in meta["classes"][k]["values"]]
    return [
        {**all_classes, "itmId": item_id, f"objL{k + 1}": codes}
        for item_id in item_ids
        for codes in chunks(split_values, MAX_CELLS // cells_per_value)
    ]


def build_hierarchy(values: list[dict[str, Any]]) -> dict[str, tuple[int, str]]:
    """분류값 코드 → (단계, 상위 경로). 예: 종로구 → (2, "서울특별시 > 종로구")"""
    by_id = {v["ITM_ID"]: v for v in values}
    result = {}
    for v in values:
        names, cur = [], v
        while cur:
            names.append(cur["ITM_NM"])
            cur = by_id.get(cur.get("UP_ITM_ID"))
        result[v["ITM_ID"]] = (len(names), " > ".join(reversed(names)))
    return result


def to_rows(records: list[dict[str, Any]], meta: dict[str, Any]) -> list[dict[str, Any]]:
    hierarchies = [build_hierarchy(c["values"]) for c in meta["classes"]]
    rows = []
    for r in records:
        dt = r.get("DT", "")
        is_number = dt.replace(".", "", 1).lstrip("-").isdigit()
        row = {
            "tbl_id": r["TBL_ID"],
            "org_id": r["ORG_ID"],
            "period": r["PRD_DE"],
            "prd_se": r["PRD_SE"],
            "item_id": r["ITM_ID"],
            "item_nm": r["ITM_NM"],
        }
        for i, h in enumerate(hierarchies, start=1):
            code = r[f"C{i}"]
            level, path = h.get(code, (None, r[f"C{i}_NM"]))
            row |= {
                f"c{i}_id": code,
                f"c{i}_nm": r[f"C{i}_NM"],
                f"c{i}_level": level,
                f"c{i}_path": path,
            }
        row |= {
            "value": dt if is_number else "",
            # 숫자가 아닌 값은 통계부호 (예: X = 비밀보호, - = 해당 없음)
            "value_flag": "" if is_number else dt,
            "unit": r.get("UNIT_NM", ""),
            "last_changed": r.get("LST_CHN_DE", ""),
        }
        rows.append(row)
    return rows


async def collect(
    client: KosisClient,
    org_id: str,
    tbl_id: str,
    prd_se: str,
    dry_run: bool = False,
    save_raw: bool = False,
) -> None:
    meta = await fetch_meta(client, org_id, tbl_id)
    start, end, n_periods = pick_period(meta["periods"], prd_se)
    requests = plan_requests(meta, n_periods)
    sizes = " × ".join(f"{c['name']} {len(c['values'])}" for c in meta["classes"])
    print(
        f"{tbl_id} {meta['tbl_nm']} | {start}~{end} | "
        f"항목 {len(meta['items'])} × {sizes} | 요청 {len(requests)}회"
    )
    if dry_run:
        return

    raw_dir = RAW_DIR / tbl_id
    if save_raw:
        raw_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for i, params in enumerate(requests, start=1):
        query = {
            "method": "getList",
            "orgId": org_id,
            "tblId": tbl_id,
            "prdSe": prd_se,
            "startPrdDe": start,
            "endPrdDe": end,
            **params,
        }
        data = await call(client, "/Param/statisticsParameterData.do", query)
        if save_raw:
            (raw_dir / f"{i:04d}.json").write_text(json.dumps(data, ensure_ascii=False))
        records.extend(data)
        print(f"  [{i}/{len(requests)}] {params['itmId']} {len(data)}건")

    rows = to_rows(records, meta)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{tbl_id}.csv"
    # utf-8-sig: 엑셀에서 열어도 한글이 깨지지 않음
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    meta_out = {
        "org_id": org_id,
        "tbl_id": tbl_id,
        "tbl_nm": meta["tbl_nm"],
        "prd_se": prd_se,
        "start": start,
        "end": end,
        "items": {x["ITM_ID"]: x["ITM_NM"] for x in meta["items"]},
        "classes": {f"c{i}": c["name"] for i, c in enumerate(meta["classes"], start=1)},
        "rows": len(rows),
        "collected_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path = OUT_DIR / f"{tbl_id}.meta.json"
    meta_path.write_text(json.dumps(meta_out, ensure_ascii=False, indent=2))
    print(f"저장: {out} ({len(rows)}행), {meta_path}")


async def collect_all(
    targets: list[tuple[str, str]], prd_se: str, dry_run: bool, save_raw: bool
) -> None:
    client = KosisClient()
    failed = []
    for org_id, tbl_id in targets:
        try:
            await collect(client, org_id, tbl_id, prd_se, dry_run, save_raw)
        except (KosisError, httpx.HTTPError) as e:
            # 한 표가 실패해도 나머지는 계속 받음
            print(f"{tbl_id} 실패: {e}")
            failed.append(tbl_id)
    print(f"완료 {len(targets) - len(failed)}개 / 실패 {len(failed)}개 {failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description="KOSIS 통계표 수집")
    parser.add_argument("org_id", nargs="?", help="기관 ID (예: 101)")
    parser.add_argument("tbl_id", nargs="?", help="통계표 ID (예: DT_1MR2060)")
    parser.add_argument("--list", type=Path, help="org_id, tbl_id 열이 있는 CSV 파일")
    parser.add_argument("--prd-se", default="Y", help="수록주기 Y/H/Q/M (기본 Y)")
    parser.add_argument("--dry-run", action="store_true", help="요청 계획만 출력")
    parser.add_argument("--save-raw", action="store_true", help="API 원본 응답도 저장 (용량 큼)")
    args = parser.parse_args()

    if args.list:
        with args.list.open(encoding="utf-8") as f:
            targets = [(r["org_id"], r["tbl_id"]) for r in csv.DictReader(f)]
    elif args.org_id and args.tbl_id:
        targets = [(args.org_id, args.tbl_id)]
    else:
        parser.error("org_id와 tbl_id를 주거나 --list를 지정하세요.")
    asyncio.run(collect_all(targets, args.prd_se, args.dry_run, args.save_raw))


if __name__ == "__main__":
    main()
