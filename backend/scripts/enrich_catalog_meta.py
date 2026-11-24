import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

from app.services.kosis_client import KosisClient
from scripts.collect_kosis import KosisError, call

CATALOG_DIR = Path("data/catalog")
CATALOG = CATALOG_DIR / "population_table_catalog.jsonl"
OUTPUT = CATALOG_DIR / "population_table_catalog_with_meta.jsonl"
CACHE_DIR = CATALOG_DIR / "meta_cache"
CMMT_CACHE_DIR = CATALOG_DIR / "cmmt_cache"
FAILURES = CATALOG_DIR / "meta_failures.jsonl"

SAMPLE_SIZE = 10
REGION_AXIS = re.compile(r"행정구역|시도|시군구|읍면동|지역|거주지")
REGION_AGGREGATES = {
    "전국",
    "동부",
    "읍부",
    "면부",
    "시부",
    "군부",
    "구부",
    "계",
    "합계",
    "소계",
    "기타",
    "미상",
}
SIDO = re.compile(
    r"(서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충청북|충청남|충북|충남|전라북|전라남"
    r"|전북|전남|경상북|경상남|경북|경남|제주)(특별시|광역시|특별자치시|특별자치도|도)?"
)
REGION_LEVELS = [
    ("시도", lambda name: bool(SIDO.fullmatch(name))),
    ("읍면동", lambda name: name[-1] in "읍면동가리"),
    ("시군구", lambda name: name[-1] in "시군구"),
]
UNIT_IN_NAME = re.compile(r"\s*\(([^()]+)\)\s*$")
UNIT_WORD = re.compile(
    r"(천|만|백만|십억|억)?(명|세|가구|호|개|건|원|년|인|쌍|배|회|일|개월|%|‰|㎢|㎡)(/[^()]+)?"
)


COMMENT_NOISE = re.compile(r"[\s\-=_~*·.]*")


def cache_path(org_id: str, tbl_id: str, kind: str = "ITM") -> Path:
    folder = CMMT_CACHE_DIR if kind == "CMMT" else CACHE_DIR
    return folder / f"{org_id}_{tbl_id}.json"


def validate(rows: list[dict[str, Any]], org_id: str, tbl_id: str) -> None:
    if not rows:
        raise KosisError("분류·항목 응답이 비어 있음")
    for row in rows:
        for key in ("OBJ_ID", "OBJ_NM", "ITM_ID", "ITM_NM"):
            if not str(row.get(key, "")).strip():
                raise KosisError(f"응답 행에 {key} 없음: {row}")
        if row.get("TBL_ID") not in (None, tbl_id) or row.get("ORG_ID") not in (None, org_id):
            raise KosisError(f"다른 표의 응답: {row.get('ORG_ID')}/{row.get('TBL_ID')}")
    if not any(row["OBJ_ID"] == "ITEM" for row in rows):
        raise KosisError("항목(OBJ_ID=ITEM)이 없음")


def split_unit(name: str, unit_nm: str | None) -> tuple[str, str | None]:
    unit = (unit_nm or "").strip() or None
    match = UNIT_IN_NAME.search(name)
    if match:
        inner = match.group(1).strip()
        if inner == unit or UNIT_WORD.fullmatch(inner):
            return UNIT_IN_NAME.sub("", name).strip(), unit or inner
    return name, unit


def region_level_names(rows: list[dict[str, Any]], depths: dict[str, int]) -> list[str] | None:
    names_by_depth: dict[int, list[str]] = {}
    for r in rows:
        value = str(r["ITM_NM"]).strip()
        if value not in REGION_AGGREGATES:
            names_by_depth.setdefault(depths[str(r["ITM_ID"])], []).append(value)
    result: list[str] = []
    for level in sorted(names_by_depth):
        values = names_by_depth[level]
        for label, matches in REGION_LEVELS:
            if label not in result and sum(map(matches, values)) * 2 > len(values):
                result.append(label)
                break
    return result or None


def summarize_axis(obj_id: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = {str(r["ITM_ID"]) for r in rows}
    parent = {
        str(r["ITM_ID"]): str(r["UP_ITM_ID"])
        for r in rows
        if r.get("UP_ITM_ID") and str(r["UP_ITM_ID"]) in ids
    }

    def depth(value_id: str) -> int:
        level, seen = 1, {value_id}
        while value_id in parent and parent[value_id] not in seen:
            value_id = parent[value_id]
            seen.add(value_id)
            level += 1
        return level

    name = str(rows[0]["OBJ_NM"]).strip()
    depths = {str(r["ITM_ID"]): depth(str(r["ITM_ID"])) for r in rows}
    levels = max(depths.values())
    is_region = bool(REGION_AXIS.search(name))
    top = [str(r["ITM_NM"]).strip() for r in rows if str(r["ITM_ID"]) not in parent]
    if is_region:
        top = [v for v in top if v not in REGION_AGGREGATES] or top
    sn = str(rows[0].get("OBJ_ID_SN", ""))
    return {
        "id": obj_id,
        "name": name,
        "order": int(sn) if sn.isdigit() else None,
        "levels": levels,
        "level_names": region_level_names(rows, depths) if is_region else None,
        "value_count": len(rows),
        "sample_values": top[:SAMPLE_SIZE],
        "values": list(dict.fromkeys(str(r["ITM_NM"]).strip() for r in rows)),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    items = []
    axes: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row["OBJ_ID"] == "ITEM":
            name, unit = split_unit(str(row["ITM_NM"]).strip(), row.get("UNIT_NM"))
            items.append({"id": row["ITM_ID"], "name": name, "unit": unit})
        else:
            axes.setdefault(row["OBJ_ID"], []).append(row)

    classifications = sorted(
        (summarize_axis(obj_id, axis_rows) for obj_id, axis_rows in axes.items()),
        key=lambda a: (a["order"] is None, a["order"] or 0, a["id"]),
    )

    def axis_text(axis: dict[str, Any]) -> str:
        if axis["level_names"]:
            return f"{axis['name']}[{'·'.join(axis['level_names'])}]"
        return axis["name"]

    item_names = [i["name"] for i in items]
    item_text = ", ".join(item_names[:5]) + (f" 등 {len(items)}개" if len(items) > 5 else "")
    return {
        "classifications": classifications,
        "items": items,
        "meta_summary": (
            f"분류: {' / '.join(axis_text(a) for a in classifications) or '없음'}"
            f" | 항목: {item_text}"
        ),
    }


def clean_comments(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[str]] = {}
    for row in rows:
        line = str(row.get("CMMT_DC") or "").strip()
        if COMMENT_NOISE.fullmatch(line):
            continue
        target = str(row.get("CMMT_NM") or "통계표").strip() or "통계표"
        groups.setdefault(target, []).append(line)
    comments = [{"target": target, "lines": lines} for target, lines in groups.items()]
    text = "\n".join(
        line if c["target"] == "통계표" else f"[{c['target']}] {line}"
        for c in comments
        for line in c["lines"]
    )
    return {"comments": comments, "comment_text": text}


async def download(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs = [
        (record, kind)
        for record in records
        for kind in ("ITM", "CMMT")
        if not cache_path(record["org_id"], record["tbl_id"], kind).exists()
    ]
    for kind in ("ITM", "CMMT"):
        left = sum(1 for _, k in jobs if k == kind)
        label = "분류·항목" if kind == "ITM" else "주석"
        done = len(records) - left
        print(f"{label}: 대상 {len(records)}개 / 받은 표 {done}개 / 새로 받을 표 {left}개")
    failures: list[dict[str, Any]] = []
    client = KosisClient()
    if jobs and not client.api_key:
        raise SystemExit(".env에 KOSIS_API_KEY가 없습니다.")
    started = time.time()
    for n, (record, kind) in enumerate(jobs, 1):
        org_id, tbl_id = record["org_id"], record["tbl_id"]
        params = {"method": "getMeta", "type": kind, "orgId": org_id, "tblId": tbl_id}
        try:
            rows = await call(client, "/statisticsData.do", params)
            if kind == "ITM":
                validate(rows, org_id, tbl_id)
            path = cache_path(org_id, tbl_id, kind)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            status = "OK" if rows or kind == "ITM" else "OK (주석 없음)"
        except (KosisError, httpx.HTTPError) as error:
            if isinstance(error, httpx.HTTPError):
                message = type(error).__name__
            else:
                message = str(error).split(" ({")[0]
            failures.append(
                {
                    **{k: record[k] for k in ("org_id", "tbl_id", "table_name")},
                    "type": kind,
                    "error": message,
                }
            )
            status = f"실패 - {message}"
            if len(failures) == n and n == 3:
                print(f"[{n}/{len(jobs)}] {tbl_id} {kind} {status}")
                raise SystemExit(
                    "처음 3개가 모두 실패했습니다. 인증키와 인터넷 연결을 확인하세요."
                ) from error
        remaining = (time.time() - started) / n * (len(jobs) - n) / 60
        print(f"[{n}/{len(jobs)}] {tbl_id} {kind} {status} (남은 시간 약 {remaining:.0f}분)")
    return failures


def build(records: list[dict[str, Any]]) -> tuple[int, int, int]:
    ok = missing = commented = 0
    tmp = OUTPUT.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            path = cache_path(record["org_id"], record["tbl_id"])
            enriched = dict(record)
            if path.exists():
                enriched.update(summarize(json.loads(path.read_text(encoding="utf-8"))))
                enriched["meta_status"] = "ok"
                ok += 1
            else:
                enriched.update(
                    {
                        "classifications": [],
                        "items": [],
                        "meta_summary": None,
                        "meta_status": "missing",
                    }
                )
                missing += 1
            cmmt_path = cache_path(record["org_id"], record["tbl_id"], "CMMT")
            if cmmt_path.exists():
                enriched.update(clean_comments(json.loads(cmmt_path.read_text(encoding="utf-8"))))
                commented += bool(enriched["comments"])
            else:
                enriched.update({"comments": None, "comment_text": None})
            stream.write(json.dumps(enriched, ensure_ascii=False, separators=(",", ":")) + "\n")
    tmp.replace(OUTPUT)
    return ok, missing, commented


def missing_cache(records: list[dict[str, Any]]) -> list[str]:
    return [r["tbl_id"] for r in records if not cache_path(r["org_id"], r["tbl_id"]).exists()]


def main() -> None:
    parser = argparse.ArgumentParser(description="통계표 카탈로그에 분류·항목·주석 메타데이터 추가")
    parser.add_argument("--limit", type=int, help="앞에서부터 N개 표만 처리 (시험용)")
    parser.add_argument("--offline", action="store_true", help="KOSIS 호출 없이 캐시로만 생성")
    args = parser.parse_args()

    if not CATALOG.exists():
        raise SystemExit(
            f"{CATALOG}이 없습니다. scripts/preprocess_population.py를 먼저 실행하세요."
        )
    with CATALOG.open(encoding="utf-8") as stream:
        records = [json.loads(line) for line in stream if line.strip()]
    if args.limit:
        records = records[: args.limit]
    if args.offline and (missing := missing_cache(records)):
        raise SystemExit(
            f"캐시 없는 표 {len(missing)}개 ({', '.join(missing[:5])} …):"
            " 결과 파일을 바꾸지 않았습니다.\n"
            "meta_cache가 있는 PC에서 실행하거나, 공유받은 결과 파일을 data/catalog/에 넣으세요."
        )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CMMT_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    failures = [] if args.offline else asyncio.run(download(records))
    ok, missing, commented = build(records)
    if not args.offline:
        with FAILURES.open("w", encoding="utf-8", newline="\n") as stream:
            for failure in failures:
                stream.write(json.dumps(failure, ensure_ascii=False) + "\n")

    print("\n완료")
    print(f"  분류·항목 붙은 표: {ok}개")
    print(f"  아직 없는 표: {missing}개" + (" (다시 실행하면 이어서 받습니다)" if missing else ""))
    print(f"  주석 있는 표: {commented}개")
    if not args.offline:
        print(f"  이번 실행 실패: {len(failures)}개 → {FAILURES}")
    print(f"  결과 파일: {OUTPUT}")


if __name__ == "__main__":
    main()
