import argparse
import asyncio
import csv
import json
import os
import re
import shutil
import time
from datetime import datetime
from itertools import product
from math import prod
from pathlib import Path
from typing import Any

import httpx

from scripts.collect_kosis import MAX_CELLS, KosisError, call, fetch_meta, to_rows

CATALOG = Path("data/catalog/population_table_catalog_with_meta.jsonl")
DATA_PATH = "/Param/statisticsParameterData.do"
WORK_DIR = "_work"
MAX_WORKERS = 8
MAX_PER_TABLE = 4
PER_MINUTE = 150
FORCE_CODES: int | None = None
MAX_CODES = 100

PERIOD_CODES = {
    "년": "Y",
    "반년": "H",
    "분기": "Q",
    "월": "M",
    "일": "D",
    "부정기": "IR",
    "IR": "IR",
}
MULTI_YEAR = re.compile(r"^\d+\s*년$")
PER_YEAR = {"Y": 1, "H": 2, "Q": 4, "M": 12, "D": 366, "F": 1, "IR": 1}
SUB_YEAR = {"H": ("01", "02"), "Q": ("01", "04"), "M": ("01", "12")}
FRACTION = re.compile(r"^\s*(\d{4})\D+(\d{1,2})\s*/\s*\d+\s*$")

BASE_COLUMNS = ["tbl_id", "org_id", "period", "prd_se", "item_id", "item_nm"]
VALUE_COLUMNS = ["value", "value_flag", "unit", "last_changed"]


def period_code(label: str) -> str | None:
    label = str(label).strip()
    if label in PERIOD_CODES:
        return PERIOD_CODES[label]
    if label in PER_YEAR:
        return label
    if MULTI_YEAR.match(label):
        return "F"
    return None


def columns(n_classes: int) -> list[str]:
    cols = list(BASE_COLUMNS)
    for i in range(1, n_classes + 1):
        cols += [f"c{i}_id", f"c{i}_nm", f"c{i}_level", f"c{i}_path"]
    return cols + VALUE_COLUMNS


def year_ranges(start: str, end: str, prd_se: str, size: int) -> list[tuple[str, str]]:
    y0, y1 = int(start[:4]), int(end[:4])
    first, last = SUB_YEAR.get(prd_se, ("", ""))
    ranges = []
    for a in range(y0, y1 + 1, size):
        b = min(a + size - 1, y1)
        ranges.append((start if a == y0 else f"{a}{first}", end if b == y1 else f"{b}{last}"))
    return ranges


def chunk(values: list[str], size: int) -> list[str]:
    return ["+".join(values[i : i + size]) for i in range(0, len(values), size)]


def split_size(cells: float, n: int) -> tuple[int, float]:
    per_value = cells / n
    size = max(1, int(MAX_CELLS // per_value)) if per_value <= MAX_CELLS else 1
    size = min(size, n)
    return size, per_value * size


def every_years(label: str) -> int:
    m = MULTI_YEAR.match(str(label).strip())
    return int(re.match(r"\d+", m.group()).group()) if m else 1


def plan_requests(
    meta: dict[str, Any], prd_se: str, start: str, end: str, every: int = 1
) -> list[dict[str, str]]:
    items = [x["ITM_ID"] for x in meta["items"]]
    classes = [[v["ITM_ID"] for v in c["values"]] for c in meta["classes"]]
    years = int(end[:4]) - int(start[:4]) + 1
    base = len(items) * prod(len(c) for c in classes)

    if prd_se == "F" and every > 1:
        n_windows = (years - 1) // every + 1
        cells = base * 2 * n_windows
        size = n_windows
        if cells > MAX_CELLS:
            size, cells = split_size(cells, n_windows)
        year_size = size * every
    else:
        cells = base * years * PER_YEAR.get(prd_se, 1)
        year_size = years
        if cells > MAX_CELLS:
            if prd_se == "D" and years > 1:
                raise KosisError("일 단위 표는 기간 나누기를 지원하지 않습니다")
            year_size, cells = split_size(cells, years)

    item_size = len(items)
    if cells > MAX_CELLS:
        item_size, cells = split_size(cells, len(items))
    item_size = min(item_size, MAX_CODES)

    class_parts: list[list[str]] = [["ALL"] for _ in classes]
    for k in sorted(range(len(classes)), key=lambda i: -len(classes[i])):
        if cells <= MAX_CELLS:
            break
        size, cells = split_size(cells, len(classes[k]))
        class_parts[k] = chunk(classes[k], min(size, MAX_CODES))
    if cells > MAX_CELLS:
        raise KosisError(f"모든 축을 나눠도 요청 하나가 {int(cells)}셀로 제한을 넘습니다")
    if FORCE_CODES:
        for k, values in enumerate(classes):
            if class_parts[k] == ["ALL"] and len(values) > FORCE_CODES:
                class_parts[k] = chunk(values, FORCE_CODES)

    sns = [int(c.get("sn") or i) for i, c in enumerate(meta["classes"], start=1)]
    requests = []
    for (s, e), ids, *objs in product(
        year_ranges(start, end, prd_se, year_size), chunk(items, item_size), *class_parts
    ):
        params = {"startPrdDe": s, "endPrdDe": e, "itmId": ids}
        params |= {f"objL{sn}": v for sn, v in zip(sns, objs, strict=True)}
        requests.append(params)
    return requests


def estimate_cells(record: dict[str, Any]) -> int:
    sizes = [c.get("value_count") or 1 for c in record.get("classifications") or []]
    base = len(record.get("items") or []) * prod(sizes)
    total = 0
    for p in record.get("catalog_periods") or []:
        code = period_code(p.get("frequency_label", "")) or "Y"
        try:
            years = int(str(p["end"])[:4]) - int(str(p["start"])[:4]) + 1
        except (KeyError, ValueError):
            years = 1
        total += base * years * PER_YEAR.get(code, 1)
    return total


def load_targets(catalog: Path) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in catalog.open(encoding="utf-8") if line.strip()]
    targets = [
        {"org_id": r["org_id"], "tbl_id": r["tbl_id"], "estimate": estimate_cells(r)}
        for r in records
    ]
    return sorted(targets, key=lambda t: (t["estimate"], t["tbl_id"]))


def append_csv(path: Path, row: dict[str, Any]) -> None:
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if new:
            writer.writeheader()
        writer.writerow(row)


def write_part(path: Path, rows: list[dict[str, Any]], cols: list[str]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def count_rows(path: Path) -> int:
    with path.open(encoding="utf-8-sig") as f:
        return sum(1 for _ in f) - 1


def request_period(period: Any, prd_se: str) -> str:
    if prd_se in ("Q", "H") and (m := FRACTION.match(str(period))):
        return f"{m[1]}{int(m[2]):02d}"
    return digits(period)


def digits(period: Any) -> str:
    return re.sub(r"\D", "", str(period))


def to_rows_by_sn(records: list[dict[str, Any]], meta: dict[str, Any]) -> list[dict[str, Any]]:
    sns = [int(c.get("sn") or i) for i, c in enumerate(meta["classes"], start=1)]
    if sns == list(range(1, len(sns) + 1)):
        return to_rows(records, meta)
    moved = []
    for r in records:
        r2 = {k: v for k, v in r.items() if not re.fullmatch(r"C\d+(_NM)?", k)}
        for i, sn in enumerate(sns, start=1):
            r2[f"C{i}"], r2[f"C{i}_NM"] = r.get(f"C{sn}"), r.get(f"C{sn}_NM")
        moved.append(r2)
    return to_rows(moved, meta)


class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.gap = 60.0 / per_minute
        self.next_at = 0.0
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.gap
        if delay > 0:
            await asyncio.sleep(delay)


LIMITER = RateLimiter(PER_MINUTE)


def done_path(out: Path, tbl_id: str) -> Path:
    return out / f"{tbl_id}.meta.json"


def merge_parts(tdir: Path, target: Path) -> int:
    parts = sorted(tdir.glob("*.csv"))
    tmp = target.with_suffix(".tmp")
    rows = 0
    with tmp.open("w", newline="", encoding="utf-8-sig") as dst:
        for k, part in enumerate(parts):
            with part.open(encoding="utf-8-sig") as src:
                header = src.readline()
                if k == 0:
                    dst.write(header)
                for line in src:
                    dst.write(line)
                    rows += 1
    tmp.replace(target)
    return rows


def plan_table(meta: dict[str, Any]) -> list[dict[str, Any]]:
    jobs = []
    for p in meta["periods"]:
        code = period_code(p["PRD_SE"])
        if code is None:
            raise KosisError(f"모르는 수록주기: {p['PRD_SE']}")
        start, end = request_period(p["STRT_PRD_DE"], code), request_period(p["END_PRD_DE"], code)
        jobs.append(
            {
                "label": p["PRD_SE"],
                "prd_se": code,
                "start": start,
                "end": end,
                "requests": plan_requests(meta, code, start, end, every_years(p["PRD_SE"])),
            }
        )
    return jobs


async def collect_table(
    client: Any,
    org_id: str,
    tbl_id: str,
    out: Path,
    dry_run: bool = False,
    per_table: int = 1,
    meta: dict[str, Any] | None = None,
    limiter: "RateLimiter | None" = None,
    pause: float | None = None,
) -> dict[str, Any]:
    if meta is None:
        meta = await fetch_meta(client, org_id, tbl_id)
    jobs = plan_table(meta)
    n_requests = sum(len(j["requests"]) for j in jobs)
    pace = {} if pause is None else {"pause": pause}
    summary = " / ".join(
        f"{j['label']} {j['start']}~{j['end']} {len(j['requests'])}회" for j in jobs
    )
    print(f"{tbl_id} {meta['tbl_nm']} | {summary}", flush=True)
    if dry_run:
        return {"requests": n_requests, "rows": 0, "sent": 0}

    tdir = out / WORK_DIR / tbl_id
    tdir.mkdir(parents=True, exist_ok=True)
    plan = [{k: j[k] for k in ("prd_se", "start", "end", "requests")} for j in jobs]
    plan_path = tdir / "plan.json"
    if plan_path.exists() and json.loads(plan_path.read_text(encoding="utf-8")) != plan:
        print(f"  {tbl_id}: 요청 계획이 바뀌어 받던 조각을 지우고 다시 받습니다", flush=True)
        shutil.rmtree(tdir)
        tdir.mkdir(parents=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    cols = columns(len(meta["classes"]))
    todo = []
    for j, job in enumerate(jobs, start=1):
        for n, params in enumerate(job["requests"], start=1):
            part = tdir / f"{j:02d}_{job['prd_se']}_{n:05d}.csv"
            if not part.exists():
                todo.append(
                    (
                        part,
                        {
                            "method": "getList",
                            "orgId": org_id,
                            "tblId": tbl_id,
                            "prdSe": job["prd_se"],
                            **params,
                        },
                    )
                )
    state = {"done": n_requests - len(todo), "sent": 0, "error": None}

    async def fetch() -> None:
        while todo and state["error"] is None:
            part, query = todo.pop(0)
            try:
                await (limiter or LIMITER).wait()
                data = await call(client, DATA_PATH, query, **pace)
                write_part(part, to_rows_by_sn(data, meta), cols)
            except Exception as e:  # noqa: BLE001 - 다른 줄도 멈추고 표를 실패로 남긴다
                state["error"] = state["error"] or e
                return
            state["sent"] += 1
            state["done"] += 1
            if n_requests > 1 and (state["done"] % 20 == 0 or state["done"] == n_requests):
                print(f"  {tbl_id} [{state['done']}/{n_requests}]", flush=True)

    await asyncio.gather(*(fetch() for _ in range(max(1, per_table))))
    if state["error"] is not None:
        raise state["error"]
    sent = state["sent"]

    rows = merge_parts(tdir, out / f"{tbl_id}.csv")
    info = {
        "org_id": org_id,
        "tbl_id": tbl_id,
        "tbl_nm": meta["tbl_nm"],
        "periods": [{k: j[k] for k in ("label", "prd_se", "start", "end")} for j in jobs],
        "items": {x["ITM_ID"]: x["ITM_NM"] for x in meta["items"]},
        "classes": {f"c{i}": c["name"] for i, c in enumerate(meta["classes"], start=1)},
        "rows": rows,
        "requests": n_requests,
        "collected_at": datetime.now().isoformat(timespec="seconds"),
    }
    tmp = out / f"{tbl_id}.meta.tmp"
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(done_path(out, tbl_id))
    shutil.rmtree(tdir)
    print(f"  저장: {tbl_id}.csv ({rows:,}행)", flush=True)
    return {"requests": n_requests, "rows": rows, "sent": sent}


async def collect_all(
    targets: list[dict[str, Any]],
    out: Path,
    dry_run: bool,
    client: Any = None,
    workers: int = 1,
    per_table: int = 1,
) -> dict[str, int]:
    if not 1 <= workers <= MAX_WORKERS:
        raise SystemExit(f"--workers는 1~{MAX_WORKERS}")
    if not 1 <= per_table <= MAX_PER_TABLE:
        raise SystemExit(f"--per-table은 1~{MAX_PER_TABLE}")
    if client is None:
        from app.services.kosis_client import KosisClient

        client = KosisClient(timeout=180.0)
    log_dir = out / WORK_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    counts = {"done": 0, "skipped": 0, "failed": 0, "planned": 0}
    total = {"requests": 0, "rows": 0}
    queue: asyncio.Queue = asyncio.Queue()
    for i, t in enumerate(targets, start=1):
        queue.put_nowait((i, t))

    async def worker() -> None:
        while not queue.empty():
            i, t = queue.get_nowait()
            tbl_id = t["tbl_id"]
            if not dry_run and done_path(out, tbl_id).exists():
                counts["skipped"] += 1
                continue
            print(f"[{i}/{len(targets)}] ", end="")
            began = time.monotonic()
            row = {"tbl_id": tbl_id, "status": "", "requests": 0, "rows": 0, "sent": 0}
            try:
                result = await collect_table(client, t["org_id"], tbl_id, out, dry_run, per_table)
                row |= result | {"status": "planned" if dry_run else "done"}
                counts[row["status"]] += 1
                total["requests"] += result["requests"]
                total["rows"] += result["rows"]
            except (KosisError, httpx.HTTPError, ValueError, KeyError, OSError) as e:
                reason = f"{type(e).__name__}: {e}"
                print(f"{tbl_id} 실패: {reason}", flush=True)
                row["status"] = "failed"
                counts["failed"] += 1
                append_csv(
                    log_dir / "failures.csv",
                    {
                        "tbl_id": tbl_id,
                        "org_id": t["org_id"],
                        "error": reason[:500],
                        "at": datetime.now().isoformat(timespec="seconds"),
                    },
                )
            row |= {
                "seconds": round(time.monotonic() - began, 1),
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            append_csv(log_dir / ("plan.csv" if dry_run else "progress.csv"), row)

    await asyncio.gather(*(worker() for _ in range(workers)))
    print(
        f"완료 {counts['done']} / 계획 {counts['planned']} / 건너뜀(이미 받음) {counts['skipped']}"
        f" / 실패 {counts['failed']} | 요청 {total['requests']:,}회, 행 {total['rows']:,}"
    )
    return counts


def is_mounted(path: Path) -> bool:
    target = str(path.resolve())
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            if any(line.split()[4] == target for line in f if len(line.split()) > 4):
                return True
    except OSError:
        pass
    return os.path.ismount(target)


def failed_ids(out: Path) -> set[str]:
    path = out / WORK_DIR / "failures.csv"
    if not path.exists():
        return set()
    with path.open(encoding="utf-8-sig") as f:
        return {r["tbl_id"] for r in csv.DictReader(f)}


def main() -> None:
    parser = argparse.ArgumentParser(description="인구 통계표 전체 수치 수집 (모든 수록주기)")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/processed"),
        help="저장 폴더. load_raw.py가 읽는 data/processed가 기본 (전체 약 120GB)",
    )
    parser.add_argument(
        "--per-table",
        type=int,
        default=4,
        help=f"표 하나 안에서 동시에 보내는 요청 수 (최대 {MAX_PER_TABLE})",
    )
    parser.add_argument(
        "--workers", type=int, default=3, help=f"동시에 받는 표 수 (최대 {MAX_WORKERS})"
    )
    parser.add_argument("--catalog", type=Path, default=CATALOG, help="표 목록 jsonl")
    parser.add_argument("--tables", nargs="+", help="이 표들만 받기 (tbl_id)")
    parser.add_argument("--limit", type=int, help="작은 표부터 N개만 (아직 안 받은 표 기준)")
    parser.add_argument("--retry-failed", action="store_true", help="failures.csv에 있는 표만 다시")
    parser.add_argument(
        "--max-codes",
        type=int,
        help="값이 이 개수보다 많은 분류는 나눠 요청 (예: 성씨·본관 표는 100)",
    )
    parser.add_argument(
        "--allow-local",
        action="store_true",
        help="저장 폴더가 별도 마운트가 아니어도 받기 (작은 시험용)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="표 구조만 조회해 요청 계획 출력 (수치는 안 받음, plan.csv에 기록)",
    )
    args = parser.parse_args()

    if not (args.dry_run or args.allow_local or is_mounted(args.out)):
        raise SystemExit(
            f"{args.out}이(가) 별도 저장소로 마운트되어 있지 않습니다"
            " (레포 폴더 = OneDrive에 저장됨).\n"
            "PowerShell에서 docker compose up -d 로 컨테이너를 켠 뒤"
            " docker compose exec로 실행하세요.\n"
            "작은 시험이라 괜찮으면 --allow-local을 붙이세요."
        )
    global FORCE_CODES
    FORCE_CODES = args.max_codes
    targets = load_targets(args.catalog)
    if args.tables:
        wanted = set(args.tables)
        targets = [t for t in targets if t["tbl_id"] in wanted]
    if args.retry_failed:
        wanted = failed_ids(args.out)
        targets = [t for t in targets if t["tbl_id"] in wanted]
    if args.limit:
        targets = [t for t in targets if not done_path(args.out, t["tbl_id"]).exists()]
        targets = targets[: args.limit]
    print(f"대상 {len(targets)}개 표 → {args.out}")
    asyncio.run(
        collect_all(targets, args.out, args.dry_run, workers=args.workers, per_table=args.per_table)
    )


if __name__ == "__main__":
    main()
