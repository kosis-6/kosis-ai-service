"""종합 프로젝트: 인구 통계표 2,364개의 수치를 모든 수록주기로 수집한다.

실전 1의 scripts/collect_kosis.py(20개 표, 년 주기만)를 전체 표용으로 넓힌 것.
표 구조 조회(fetch_meta)와 응답 → 행 변환(to_rows)은 collect_kosis.py를 그대로 쓰므로,
결과 CSV·meta.json 모양이 같아 pipeline/load_raw.py로 바로 PostgreSQL에 넣을 수 있다.

달라진 점
  - 표마다 KOSIS가 알려 주는 수록주기(년·5년·분기·월 등)를 전부 받는다
  - 4만 셀 제한을 넘으면 기간 → 항목 → 분류 순서로 나눠 요청한다
  - 요청 조각마다 바로 저장해서, 끊겨도 받은 조각은 남고 같은 명령으로 이어 받는다
  - 표 목록을 예상 크기가 작은 순서로 받는다 (작은 표 먼저, 대형 표 마지막)
  - 표 여러 개를 동시에 받는다 (--workers, 최대 8. 요청은 합쳐서 분당 150회 이하)
  - 큰 표는 표 안에서도 요청을 동시에 보낸다 (--per-table, 기본 4)

입력: data/catalog/population_table_catalog_with_meta.jsonl (표 목록, 예상 크기 계산용)
출력: --out 폴더 (기본 data/processed, 전체 약 120GB)
  {tbl_id}.csv, {tbl_id}.meta.json    다 받은 표 (load_raw.py 입력 형식). meta.json이 있으면 완료
  _work/{tbl_id}/                     받는 중인 표의 조각. 다 받으면 합친 뒤 지운다
  _work/progress.csv                  표마다 결과, 요청 수, 행 수, 걸린 시간
  _work/failures.csv                  실패한 표와 이유

실행 (backend/ 에서)
  python -m scripts.collect_all --dry-run --limit 20     # 요청 계획만 (수치는 안 받음)
  python -m scripts.collect_all --limit 10               # 작은 표 10개로 시험
  python -m scripts.collect_all                          # 전체 (끊기면 같은 명령으로 이어 받기)
  python -m scripts.collect_all --retry-failed           # 실패한 표만 다시
  python -m scripts.collect_all --tables DT_1B04006      # 특정 표만

수록주기 코드: 년 Y, 반년 H, 분기 Q, 월 M, 일 D는 실전 1과 같다.
  다년(2년·5년 등)은 F, 부정기는 IR로 요청하는데, KOSIS 문서에서 코드표를 확인하지 못했다.
  틀리면 해당 표가 failures.csv에 남으므로 --dry-run이나 시험 수집에서 먼저 확인한다.
"""

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
WORK_DIR = "_work"  # 받는 중인 조각·진행 기록 (load_raw.py는 하위 폴더를 읽지 않음)
MAX_WORKERS = 8
MAX_PER_TABLE = 4  # 표 하나 안에서 동시에 보내는 요청 수 상한 (KOSIS 서버 부담)
PER_MINUTE = 150  # 데이터 요청 상한 (KOSIS 제한 분당 200회보다 낮게)
FORCE_CODES: int | None = None  # --max-codes: 값이 이보다 많은 분류는 셀 수와 상관없이 나눔
MAX_CODES = 100  # 한 요청에 +로 잇는 코드 수 상한 (URL 길이 제한 대비)

# KOSIS 수록주기 이름 → 요청 코드
PERIOD_CODES = {"년": "Y", "반년": "H", "분기": "Q", "월": "M", "일": "D", "부정기": "IR", "IR": "IR"}
MULTI_YEAR = re.compile(r"^\d+\s*년$")  # 2년, 5년, 10년 → F
# 1년당 시점 수 (셀 수 계산용. 다년·부정기는 1로 잡아 넉넉하게 계산)
PER_YEAR = {"Y": 1, "H": 2, "Q": 4, "M": 12, "D": 366, "F": 1, "IR": 1}
# 연도 안의 첫·마지막 시점 표기 (기간을 연 단위로 나눌 때 씀. 예: 월 201101 ~ 201112)
SUB_YEAR = {"H": ("1", "2"), "Q": ("1", "4"), "M": ("01", "12")}

BASE_COLUMNS = ["tbl_id", "org_id", "period", "prd_se", "item_id", "item_nm"]
VALUE_COLUMNS = ["value", "value_flag", "unit", "last_changed"]


def period_code(label: str) -> str | None:
    label = str(label).strip()
    if label in PERIOD_CODES:
        return PERIOD_CODES[label]
    if label in PER_YEAR:  # 응답이 이미 코드로 오는 경우
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
    """수록 기간을 size년씩 나눈다. 처음과 끝은 KOSIS가 준 시점을 그대로 쓴다."""
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
    """cells를 n개 값으로 이루어진 축 하나로 나눌 때 한 번에 묶을 값 수와 그때의 셀 수."""
    per_value = cells / n
    size = max(1, int(MAX_CELLS // per_value)) if per_value <= MAX_CELLS else 1
    size = min(size, n)
    return size, per_value * size


def every_years(label: str) -> int:
    """다년 주기 표의 간격 (5년 → 5). 다년이 아니면 1"""
    m = MULTI_YEAR.match(str(label).strip())
    return int(re.match(r"\d+", m.group()).group()) if m else 1


def plan_requests(
    meta: dict[str, Any], prd_se: str, start: str, end: str, every: int = 1
) -> list[dict[str, str]]:
    """한 수록주기를 받을 요청 목록. 4만 셀 이하가 되도록 기간 → 항목 → 분류 순서로 나눈다.

    every: 다년 주기(F)의 간격. 5년 주기 표는 5년에 시점이 하나뿐이라, 매년 있다고 보고 나누면
    요청의 4/5가 빈 기간을 묻게 된다. 그래서 기간은 every년 단위로 나눈다.
    """
    items = [x["ITM_ID"] for x in meta["items"]]
    classes = [[v["ITM_ID"] for v in c["values"]] for c in meta["classes"]]
    years = int(end[:4]) - int(start[:4]) + 1
    base = len(items) * prod(len(c) for c in classes)  # 시점 하나의 셀 수

    if prd_se == "F" and every > 1:
        # 조사 연도가 불규칙할 수 있다 (인구총조사: 1940, 1944 / 1960, 1966). every년 구간 하나에
        # 시점이 2개까지 들어갈 수 있다고 보고 셀 수를 2배로 잡는다 (매년으로 보는 것보다 훨씬 적음)
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
        # 성씨·본관(1만 5천 개)처럼 값이 아주 많은 분류는 셀 수가 적어도 ALL로 한 번에 요청하면
        # KOSIS 서버 오류(50)가 난다. 값 목록을 URL에 넣어 나누므로 100개 정도가 한계 (그 이상은 413/414)
        for k, values in enumerate(classes):
            if class_parts[k] == ["ALL"] and len(values) > FORCE_CODES:
                class_parts[k] = chunk(values, FORCE_CODES)

    # objL 번호는 분류 순번(OBJ_ID_SN). 순번이 비는 표(예: 2~5)는 있는 순번에만 보낸다.
    # 빈 자리(objL1)까지 ALL로 채우면 KOSIS가 '잘못된 요청 변수'(21)로 거절한다 (DT_1PM2003에서 확인)
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
    """카탈로그 메타데이터로 표 크기를 대략 계산한다 (받는 순서를 정하는 데만 씀)."""
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
    """임시 파일에 다 쓴 뒤 이름을 바꿔서, 쓰다 끊긴 조각이 완성본으로 남지 않게 한다."""
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)


def count_rows(path: Path) -> int:
    with path.open(encoding="utf-8-sig") as f:
        return sum(1 for _ in f) - 1


def digits(period: Any) -> str:
    """KOSIS 수록 시점 표기에서 숫자만 남긴다. 월·분기 표는 '1970.01'처럼 점이 오는데,
    요청에는 숫자만 써야 한다 (오류 21)"""
    return re.sub(r"\D", "", str(period))


def to_rows_by_sn(records: list[dict[str, Any]], meta: dict[str, Any]) -> list[dict[str, Any]]:
    """응답의 분류 칸 C{n}은 분류 순번(OBJ_ID_SN)이다. 순번이 비는 표(예: 1, 3)는
    collect_kosis.to_rows가 C1, C2로 읽어 틀리므로, 순번 칸을 앞에서부터 C1, C2로 옮긴 뒤 넘긴다"""
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
    """모든 일꾼을 합쳐 요청 시작 간격을 60/PER_MINUTE초 이상으로 벌린다"""

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
    """다 받은 표 표시. load_raw.py가 읽는 {tbl_id}.meta.json을 마지막에 쓰므로 그것으로 판단"""
    return out / f"{tbl_id}.meta.json"


def merge_parts(tdir: Path, target: Path) -> int:
    """조각 CSV를 표 하나의 CSV로 합친다 (헤더는 한 번만). 다 쓴 뒤 이름을 바꾼다."""
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
    """표 하나를 받을 요청 계획: 수록주기마다 {label, prd_se, start, end, requests}.
    요청 수(len(requests)의 합)로 질문 처리 중에 바로 받을 표인지 정한다
    (app/services/table_cache.py)
    """
    jobs = []
    for p in meta["periods"]:
        code = period_code(p["PRD_SE"])
        if code is None:
            raise KosisError(f"모르는 수록주기: {p['PRD_SE']}")
        start, end = digits(p["STRT_PRD_DE"]), digits(p["END_PRD_DE"])
        jobs.append({"label": p["PRD_SE"], "prd_se": code, "start": start, "end": end,
                     "requests": plan_requests(meta, code, start, end, every_years(p["PRD_SE"]))})
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
    """표 하나를 받는다. 반환: 요청 수, 행 수, 이번에 새로 보낸 요청 수.

    per_table: 표 하나 안에서 동시에 보내는 요청 수. 큰 표는 요청 하나에 10초 넘게 걸려서
    하나씩 보내면 표 하나가 하루 넘게 걸린다. 조각은 서로 독립이라 순서와 상관없이 받아도 된다.
    meta: 이미 받은 fetch_meta 결과 (없으면 받는다). limiter: 요청 간격 (없으면 이 모듈의 LIMITER).
    pause: 요청마다 응답 뒤 쉬는 시간 (없으면 collect_kosis.call 기본값)
    """
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

    # 받는 중인 조각은 _work/{tbl_id}/에 두고, 다 받으면 {tbl_id}.csv 하나로 합친다
    tdir = out / WORK_DIR / tbl_id
    tdir.mkdir(parents=True, exist_ok=True)
    plan = [{k: j[k] for k in ("prd_se", "start", "end", "requests")} for j in jobs]
    plan_path = tdir / "plan.json"
    if plan_path.exists() and json.loads(plan_path.read_text(encoding="utf-8")) != plan:
        # 요청 나누는 방법이 바뀌면 지난 조각과 맞지 않으므로 지우고 처음부터 받는다
        print(f"  {tbl_id}: 요청 계획이 바뀌어 받던 조각을 지우고 다시 받습니다", flush=True)
        shutil.rmtree(tdir)
        tdir.mkdir(parents=True)
    plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    cols = columns(len(meta["classes"]))
    todo = []  # 아직 안 받은 조각 (지난번에 받은 조각은 건너뜀)
    for j, job in enumerate(jobs, start=1):
        for n, params in enumerate(job["requests"], start=1):
            part = tdir / f"{j:02d}_{job['prd_se']}_{n:05d}.csv"
            if not part.exists():
                todo.append((part, {"method": "getList", "orgId": org_id, "tblId": tbl_id,
                                    "prdSe": job["prd_se"], **params}))
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
        raise state["error"]  # 받은 조각은 남아 있어 다음 실행 때 이어서 받는다
    sent = state["sent"]

    rows = merge_parts(tdir, out / f"{tbl_id}.csv")
    # load_raw.py가 읽는 형식 (collect_kosis.py의 .meta.json과 같은 칸 + 주기 목록)
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
    shutil.rmtree(tdir)  # 합쳤으니 조각은 지운다 (용량 두 배 방지)
    print(f"  저장: {tbl_id}.csv ({rows:,}행)", flush=True)
    return {"requests": n_requests, "rows": rows, "sent": sent}


async def collect_all(
    targets: list[dict[str, Any]], out: Path, dry_run: bool, client: Any = None, workers: int = 1,
    per_table: int = 1,
) -> dict[str, int]:
    """표 여러 개를 workers개씩 동시에 받는다.

    큰 표는 요청 하나에 10초 넘게 걸려 일꾼이 많을수록 빨라진다. 데이터 요청은 모든 일꾼을
    합쳐 RateLimiter로 분당 PER_MINUTE회 아래로 묶어 KOSIS 제한(200회)을 넘지 않는다.
    """
    if not 1 <= workers <= MAX_WORKERS:
        raise SystemExit(f"--workers는 1~{MAX_WORKERS}")
    if not 1 <= per_table <= MAX_PER_TABLE:
        raise SystemExit(f"--per-table은 1~{MAX_PER_TABLE}")
    if client is None:
        from app.services.kosis_client import KosisClient

        # 성씨·본관처럼 한 번에 받는 칸이 많은 표는 응답이 30초(기본값)를 넘을 수 있다
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
                # 한 표가 실패해도 나머지는 계속 받는다
                # 시간 초과 같은 오류는 메시지가 비어 있어 오류 종류를 함께 남긴다
                reason = f"{type(e).__name__}: {e}"
                print(f"{tbl_id} 실패: {reason}", flush=True)
                row["status"] = "failed"
                counts["failed"] += 1
                append_csv(log_dir / "failures.csv",
                           {"tbl_id": tbl_id, "org_id": t["org_id"], "error": reason[:500],
                            "at": datetime.now().isoformat(timespec="seconds")})
            row |= {"seconds": round(time.monotonic() - began, 1),
                    "at": datetime.now().isoformat(timespec="seconds")}
            append_csv(log_dir / ("plan.csv" if dry_run else "progress.csv"), row)

    await asyncio.gather(*(worker() for _ in range(workers)))
    print(f"완료 {counts['done']} / 계획 {counts['planned']} / 건너뜀(이미 받음) {counts['skipped']}"
          f" / 실패 {counts['failed']} | 요청 {total['requests']:,}회, 행 {total['rows']:,}")
    return counts


def is_mounted(path: Path) -> bool:
    """path가 별도 저장소로 마운트됐는지. Windows Docker 바인드 마운트는 부모와 장치 번호가 같아
    os.path.ismount가 False를 돌려주므로 /proc/self/mountinfo의 마운트 위치(5번째 칸)로 판단한다"""
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
    parser.add_argument("--out", type=Path, default=Path("data/processed"),
                        help="저장 폴더. load_raw.py가 읽는 data/processed가 기본 (전체 약 120GB)")
    parser.add_argument("--per-table", type=int, default=4,
                        help=f"표 하나 안에서 동시에 보내는 요청 수 (최대 {MAX_PER_TABLE})")
    parser.add_argument("--workers", type=int, default=3, help=f"동시에 받는 표 수 (최대 {MAX_WORKERS})")
    parser.add_argument("--catalog", type=Path, default=CATALOG, help="표 목록 jsonl")
    parser.add_argument("--tables", nargs="+", help="이 표들만 받기 (tbl_id)")
    parser.add_argument("--limit", type=int, help="작은 표부터 N개만 (아직 안 받은 표 기준)")
    parser.add_argument("--retry-failed", action="store_true", help="failures.csv에 있는 표만 다시")
    parser.add_argument("--max-codes", type=int,
                        help="값이 이 개수보다 많은 분류는 나눠 요청 (예: 성씨·본관 표는 100)")
    parser.add_argument("--allow-local", action="store_true",
                        help="저장 폴더가 별도 마운트가 아니어도 받기 (작은 시험용)")
    parser.add_argument("--dry-run", action="store_true",
                        help="표 구조만 조회해 요청 계획 출력 (수치는 안 받음, plan.csv에 기록)")
    args = parser.parse_args()

    # 전체 약 120GB라 OneDrive 안(레포 폴더)에 쌓이면 안 된다. 컨테이너에서는 compose.override.yaml이
    # C:\\kosis_data를 data/processed에 마운트해야 한다. VS Code Dev Container는 이 파일을 안 읽는다
    if not (args.dry_run or args.allow_local or is_mounted(args.out)):
        raise SystemExit(
            f"{args.out}이(가) 별도 저장소로 마운트되어 있지 않습니다 (레포 폴더 = OneDrive에 저장됨).\n"
            "PowerShell에서 docker compose up -d 로 컨테이너를 켠 뒤 docker compose exec로 실행하세요.\n"
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
    asyncio.run(collect_all(targets, args.out, args.dry_run, workers=args.workers,
                            per_table=args.per_table))


if __name__ == "__main__":
    main()
