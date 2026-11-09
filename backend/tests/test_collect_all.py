import asyncio
import csv
import json
import time
from itertools import product
from pathlib import Path

import pytest

from scripts import collect_all as ca
from scripts import collect_kosis


class FakeKosis:
    """KOSIS 응답 흉내. getList는 요청한 항목·분류·기간 조합만 돌려준다."""

    def __init__(self, items, classes, periods, fail_on: int | None = None, sns=None, dotted=False):
        self.items = items  # ["T1", "T2"]
        self.classes = classes  # [["11", "21"], ["1", "2"]]
        self.periods = periods  # [("년", "2020", "2023"), ("월", "202301", "202312")]
        self.fail_on = fail_on
        self.sns = sns or list(range(1, len(classes) + 1))  # 분류 순번 (빈 자리 있을 수 있음)
        self.dotted = dotted  # 수록 시점을 '2023.01'처럼 점으로 보내는 표
        self.data_calls = 0

    def expand(self, prd_se, start, end):
        if prd_se == "M":
            out, y, m = [], int(start[:4]), int(start[4:])
            while f"{y}{m:02d}" <= end:
                out.append(f"{y}{m:02d}")
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            return out
        if prd_se == "F":  # 5년 주기: 표 수록 시작 연도부터 5년마다 있는 시점만
            anchor = int(next(s for label, s, _ in self.periods if label not in ("년", "월")))
            return [str(y) for y in range(int(start), int(end) + 1) if (y - anchor) % 5 == 0]
        return [str(y) for y in range(int(start), int(end) + 1)]

    async def get(self, path, params):
        if params["method"] == "getMeta":
            if params["type"] == "TBL":
                return [{"TBL_NM": "가짜 표"}]
            if params["type"] == "PRD":
                dot = (lambda v: f"{v[:4]}.{v[4:]}" if len(v) > 4 else v) if self.dotted else str
                return [{"PRD_SE": label, "STRT_PRD_DE": dot(s), "END_PRD_DE": dot(e)}
                        for label, s, e in self.periods]
            rows = [{"OBJ_ID": "ITEM", "ITM_ID": i, "ITM_NM": f"항목{i}", "UNIT_NM": "명"}
                    for i in self.items]
            for k, values in zip(self.sns, self.classes, strict=True):
                rows += [{"OBJ_ID": f"A{k}", "OBJ_ID_SN": str(k), "OBJ_NM": f"분류{k}",
                          "ITM_ID": v, "ITM_NM": f"값{v}", "UP_ITM_ID": None} for v in values]
            return rows
        self.data_calls += 1
        if self.fail_on == self.data_calls:
            raise collect_kosis.KosisError("일부러 실패")
        if not (params["startPrdDe"].isdigit() and params["endPrdDe"].isdigit()):
            raise collect_kosis.KosisError("21: 수록 시점에는 숫자만")
        for k in self.sns:  # 실제 KOSIS: 있는 순번마다 objL 필요
            if f"objL{k}" not in params:
                raise collect_kosis.KosisError("20: 필수요청변수값이 누락되었습니다. (objL)")
        for k in range(1, 9):  # 없는 순번에 objL을 보내면 거절 (DT_1PM2003)
            if f"objL{k}" in params and k not in self.sns:
                raise collect_kosis.KosisError("21: 잘못된 요청 변수를 호출 하였습니다.")
        items = self.items if params["itmId"] == "ALL" else params["itmId"].split("+")
        axes = []
        for k, values in zip(self.sns, self.classes, strict=True):
            v = params[f"objL{k}"]
            axes.append(values if v == "ALL" else v.split("+"))
        periods = self.expand(params["prdSe"], params["startPrdDe"], params["endPrdDe"])
        records = []
        for prd, item, *codes in product(periods, items, *axes):
            r = {"TBL_ID": params["tblId"], "ORG_ID": params["orgId"], "PRD_DE": prd,
                 "PRD_SE": params["prdSe"], "ITM_ID": item, "ITM_NM": f"항목{item}",
                 "DT": "1", "UNIT_NM": "명", "LST_CHN_DE": "2026-01-01"}
            for k, c in zip(self.sns, codes, strict=True):  # 응답 칸 번호도 순번
                r[f"C{k}"], r[f"C{k}_NM"] = c, f"값{c}"
            records.append(r)
        return records


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(collect_kosis, "REQUEST_INTERVAL", 0)
    monkeypatch.setattr(ca, "LIMITER", ca.RateLimiter(per_minute=6_000_000))


def read_rows(out: Path, tbl_id: str = "DT_TEST") -> list[dict]:
    with (out / f"{tbl_id}.csv").open(encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def run(client, out, dry_run=False, tables=("DT_TEST",), workers=1, per_table=1):
    targets = [{"org_id": "101", "tbl_id": t, "estimate": 0} for t in tables]
    return asyncio.run(ca.collect_all(targets, out, dry_run, client=client, workers=workers,
                                      per_table=per_table))


def test_period_code():
    assert ca.period_code("년") == "Y"
    assert ca.period_code("월") == "M"
    assert ca.period_code("5년") == "F"
    assert ca.period_code("부정기") == "IR"
    assert ca.period_code("Q") == "Q"
    assert ca.period_code("이상한주기") is None


def test_year_ranges_keep_kosis_bounds():
    assert ca.year_ranges("201103", "201306", "M", 1) == [
        ("201103", "201112"), ("201201", "201212"), ("201301", "201306")]
    assert ca.year_ranges("2015", "2025", "F", 5) == [
        ("2015", "2019"), ("2020", "2024"), ("2025", "2025")]


def test_small_table_one_request(tmp_path):
    client = FakeKosis(["T1", "T2"], [["11", "21"]], [("년", "2020", "2023")])
    run(client, tmp_path)
    assert client.data_calls == 1
    assert len(read_rows(tmp_path)) == 2 * 2 * 4
    # load_raw.py 입력 형식: 표 CSV와 meta.json만 남고 조각 폴더는 지워짐
    meta = json.loads((tmp_path / "DT_TEST.meta.json").read_text(encoding="utf-8"))
    assert {"org_id", "tbl_id", "tbl_nm", "classes"} <= meta.keys()
    assert meta["classes"] == {"c1": "분류1"}
    assert not (tmp_path / "_work" / "DT_TEST").exists()


def test_all_periods_and_split_cover_every_cell_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "MAX_CELLS", 50)
    client = FakeKosis(["T1", "T2", "T3"], [["11", "21", "22"], ["1", "2"]],
                       [("년", "2015", "2024"), ("월", "202301", "202412"), ("5년", "2010", "2020")])
    run(client, tmp_path)
    rows = read_rows(tmp_path)
    keys = [(r["prd_se"], r["period"], r["item_id"], r["c1_id"], r["c2_id"]) for r in rows]
    assert len(keys) == len(set(keys))  # 겹친 요청 없음
    expected = 3 * 3 * 2 * (10 + 24 + 3)
    assert len(rows) == expected  # 빠진 칸 없음
    assert client.data_calls > 1
    meta = json.loads((tmp_path / "DT_TEST.meta.json").read_text(encoding="utf-8"))
    assert meta["rows"] == expected
    assert [p["prd_se"] for p in meta["periods"]] == ["Y", "M", "F"]


def test_plan_requests_stay_under_limit(monkeypatch):
    monkeypatch.setattr(ca, "MAX_CELLS", 40)
    meta = {"items": [{"ITM_ID": f"T{i}"} for i in range(5)],
            "classes": [{"values": [{"ITM_ID": str(v)} for v in range(30)]}]}
    for params in ca.plan_requests(meta, "Y", "2000", "2009"):
        n_items = len(params["itmId"].split("+"))
        n_vals = 30 if params["objL1"] == "ALL" else len(params["objL1"].split("+"))
        years = int(params["endPrdDe"]) - int(params["startPrdDe"]) + 1
        assert n_items * n_vals * years <= 40


def test_resume_sends_only_missing_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "MAX_CELLS", 10)
    args = (["T1", "T2"], [["11", "21"]], [("년", "2015", "2024")])
    first = FakeKosis(*args, fail_on=3)
    counts = run(first, tmp_path)
    assert counts["failed"] == 1
    assert not (tmp_path / "DT_TEST.meta.json").exists()
    assert (tmp_path / "_work" / "failures.csv").exists()

    second = FakeKosis(*args)
    run(second, tmp_path)
    total = first.data_calls - 1 + second.data_calls  # 첫 실행의 실패한 요청은 저장 안 됨
    assert second.data_calls < total
    assert len(read_rows(tmp_path)) == 2 * 2 * 10

    third = FakeKosis(*args)
    counts = run(third, tmp_path)
    assert counts["skipped"] == 1 and third.data_calls == 0


def test_dry_run_saves_no_data(tmp_path):
    client = FakeKosis(["T1"], [["11"]], [("년", "2020", "2021")])
    counts = run(client, tmp_path, dry_run=True)
    assert counts["planned"] == 1 and client.data_calls == 0
    assert not (tmp_path / "DT_TEST.csv").exists()
    assert (tmp_path / "_work" / "plan.csv").exists()


def test_unknown_period_is_recorded_as_failure(tmp_path):
    client = FakeKosis(["T1"], [["11"]], [("이상한주기", "2020", "2021")])
    counts = run(client, tmp_path)
    assert counts["failed"] == 1
    text = (tmp_path / "_work" / "failures.csv").read_text(encoding="utf-8-sig")
    assert "모르는 수록주기" in text


def test_load_targets_orders_small_first(tmp_path):
    catalog = tmp_path / "c.jsonl"
    big = {"org_id": "101", "tbl_id": "BIG", "items": [{}] * 3,
           "classifications": [{"value_count": 300}],
           "catalog_periods": [{"frequency_label": "월", "start": "201001", "end": "202012"}]}
    small = {"org_id": "101", "tbl_id": "SMALL", "items": [{}],
             "classifications": [{"value_count": 18}],
             "catalog_periods": [{"frequency_label": "5년", "start": "2010", "end": "2020"}]}
    catalog.write_text("\n".join(json.dumps(r) for r in (big, small)), encoding="utf-8")
    assert [t["tbl_id"] for t in ca.load_targets(catalog)] == ["SMALL", "BIG"]


def test_parallel_workers_collect_every_table(tmp_path):
    client = FakeKosis(["T1"], [["11", "21"]], [("년", "2020", "2022")])
    tables = [f"DT_{i}" for i in range(7)]
    counts = run(client, tmp_path, tables=tables, workers=3)
    assert counts["done"] == 7
    for t in tables:
        assert len(read_rows(tmp_path, t)) == 1 * 2 * 3


def test_too_many_workers_rejected(tmp_path):
    with pytest.raises(SystemExit):
        run(FakeKosis(["T1"], [["11"]], [("년", "2020", "2020")]), tmp_path, workers=9)


def test_rate_limiter_spaces_requests(monkeypatch):
    starts = []

    async def go():
        limiter = ca.RateLimiter(per_minute=600)  # 0.1초 간격

        async def one():
            await limiter.wait()
            starts.append(time.monotonic())

        await asyncio.gather(*(one() for _ in range(5)))

    asyncio.run(go())
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    assert min(gaps) >= 0.09


def test_class_sequence_gap_uses_sn_for_objl_and_columns(tmp_path, monkeypatch):
    # DT_1BDOO14처럼 분류 순번이 1, 3인 표
    monkeypatch.setattr(ca, "MAX_CELLS", 20)
    client = FakeKosis(["T1", "T2"], [["11", "21", "22"], ["7", "8"]],
                       [("년", "2018", "2020")], sns=[1, 3])
    counts = run(client, tmp_path)
    assert counts["done"] == 1
    rows = read_rows(tmp_path)
    assert len(rows) == 2 * 3 * 2 * 3
    assert {r["c1_id"] for r in rows} == {"11", "21", "22"}
    assert {r["c2_id"] for r in rows} == {"7", "8"}


def test_dotted_periods_sent_as_digits(tmp_path):
    # DT_1B80030처럼 수록 시점이 '1970.01'로 오는 월 표
    client = FakeKosis(["T1"], [["11"]], [("월", "202301", "202306")], dotted=True)
    counts = run(client, tmp_path)
    assert counts["done"] == 1
    assert len(read_rows(tmp_path)) == 6


def test_five_year_tables_skip_empty_years(tmp_path, monkeypatch):
    # 인구총조사처럼 5년 주기·큰 표: 매년으로 보고 나누면 요청 4/5가 빈 기간
    monkeypatch.setattr(ca, "MAX_CELLS", 12)
    client = FakeKosis(["T1"], [["11", "21", "22", "23", "24", "25"]], [("5년", "1990", "2020")])
    run(client, tmp_path)
    rows = read_rows(tmp_path)
    assert len(rows) == 6 * 7  # 1990~2020 시점 7개 빠짐없이
    assert client.data_calls == 7  # 5년 구간마다 1번 (매년으로 보면 31번)


def test_irregular_census_years_stay_under_limit(tmp_path, monkeypatch):
    # 1940, 1944처럼 5년 구간 하나에 시점이 2개 → 요청 하나가 제한을 넘으면 안 됨
    monkeypatch.setattr(ca, "MAX_CELLS", 12)
    values = [{"ITM_ID": str(v)} for v in range(6)]
    meta = {"items": [{"ITM_ID": "T1"}], "classes": [{"values": values}]}
    for params in ca.plan_requests(meta, "F", "1925", "1949", every=5):
        s, e = int(params["startPrdDe"]), int(params["endPrdDe"])
        census = [y for y in (1925, 1930, 1935, 1940, 1944, 1949) if s <= y <= e]
        n_vals = 6 if params["objL1"] == "ALL" else len(params["objL1"].split("+"))
        assert len(census) * n_vals <= 12


def test_changed_plan_restarts_table(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "MAX_CELLS", 10)
    args = (["T1", "T2"], [["11", "21"]], [("년", "2015", "2024")])
    run(FakeKosis(*args, fail_on=3), tmp_path)  # 조각 일부만 받은 상태
    monkeypatch.setattr(ca, "MAX_CELLS", 20)  # 나누는 방법이 바뀜
    counts = run(FakeKosis(*args), tmp_path)
    assert counts["done"] == 1
    assert len(read_rows(tmp_path)) == 2 * 2 * 10


def test_parallel_requests_inside_table(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "MAX_CELLS", 10)
    client = FakeKosis(["T1", "T2"], [["11", "21", "22"]], [("년", "2011", "2024")])
    counts = run(client, tmp_path, per_table=4)
    assert counts["done"] == 1
    rows = read_rows(tmp_path)
    keys = [(r["period"], r["item_id"], r["c1_id"]) for r in rows]
    assert len(keys) == len(set(keys)) == 2 * 3 * 14


def test_parallel_failure_keeps_parts_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "MAX_CELLS", 10)
    args = (["T1", "T2"], [["11", "21"]], [("년", "2011", "2024")])
    counts = run(FakeKosis(*args, fail_on=5), tmp_path, per_table=3)
    assert counts["failed"] == 1
    saved = len(list((tmp_path / "_work" / "DT_TEST").glob("*.csv")))
    assert saved >= 3  # 실패 전까지 받은 조각은 남음
    second = FakeKosis(*args)
    run(second, tmp_path, per_table=3)
    assert len(read_rows(tmp_path)) == 2 * 2 * 14
    assert second.data_calls <= 14 - saved + 1


def test_max_codes_splits_large_class_even_if_cells_fit(tmp_path, monkeypatch):
    monkeypatch.setattr(ca, "FORCE_CODES", 4)
    client = FakeKosis(["T1"], [[str(v) for v in range(10)]], [("년", "2015", "2015")])
    run(client, tmp_path)
    assert client.data_calls == 3  # 10개를 4개씩 → 3번
    assert len(read_rows(tmp_path)) == 10


def test_class_sequence_starting_at_two(tmp_path, monkeypatch):
    # DT_1PM2003처럼 분류 순번이 2~5: objL1을 보내면 KOSIS가 거절
    monkeypatch.setattr(ca, "MAX_CELLS", 20)
    client = FakeKosis(["T1"], [["11", "21", "22"], ["0", "1", "2"]], [("5년", "2020", "2020")],
                       sns=[2, 3])
    counts = run(client, tmp_path)
    assert counts["done"] == 1
    assert len(read_rows(tmp_path)) == 3 * 3
