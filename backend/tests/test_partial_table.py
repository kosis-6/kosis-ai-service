"""적재 안 된 큰 표에서 칸만 받아 답하기
(app/services/partial_table.py, dynamic_indicator._build_partial)
DB·KOSIS 없이 만든 표의 메타와 응답을 바꿔 끼운다
"""

import asyncio

import pytest

from app.core.stats_db import StatsTableMissing
from app.services import dynamic_indicator as d
from app.services import flow, indicators, load_queue, lookup, partial_table, table_cache
from scripts import collect_all, collect_kosis

TBL = "DT_TEST_EDU"
YEARS = range(2015, 2021)
REGIONS = {"00": "전국", "11": "서울특별시", "21": "부산광역시"}
SEXES = {"0": "계", "1": "남자", "2": "여자"}
EDUS = {"00": "계", "01": "초등학교", "02": "중학교", "03": "고등학교"}
FLAGGED = (2016, "21", "2", "01")  # 비공개(X)인 칸


def _values(names: dict[str, str], parent: str | None = None) -> list[dict]:
    first = next(iter(names))
    return [
        {"ITM_ID": c, "ITM_NM": n, "UP_ITM_ID": None if c == first else parent or first}
        for c, n in names.items()
    ]


def make_meta(periods=None) -> dict:
    return {
        "tbl_nm": "행정구역별 교육정도별 인구(시험)",
        "items": [{"OBJ_ID": "ITEM", "ITM_ID": "T10", "ITM_NM": "인구", "UNIT_NM": "명"}],
        "classes": [
            {"sn": 1, "name": "행정구역별", "values": _values(REGIONS)},
            {"sn": 2, "name": "성별", "values": _values(SEXES)},
            {"sn": 3, "name": "교육정도별", "values": _values(EDUS)},
        ],
        "periods": periods or [{"PRD_SE": "년", "STRT_PRD_DE": "2015", "END_PRD_DE": "2020"}],
    }


def leaf(year: int, region: str, sex: str, edu: str) -> int:
    return (year - 2000) + 10 * int(edu) + 100 * int(sex) + 1000 * (1 if region == "11" else 2)


def value(year: int, region: str, sex: str, edu: str) -> int:
    """합계 칸은 아래 칸의 합 (KOSIS 표처럼)"""
    regions = [r for r in REGIONS if r != "00"] if region == "00" else [region]
    sexes = [s for s in SEXES if s != "0"] if sex == "0" else [sex]
    edus = [e for e in EDUS if e != "00"] if edu == "00" else [edu]
    return sum(leaf(year, r, s, e) for r in regions for s in sexes for e in edus)


def record(year, region, sex, edu, prd_se="A", period=None) -> dict:
    dt = "X" if (year, region, sex, edu) == FLAGGED else str(value(year, region, sex, edu))
    return {
        "TBL_ID": TBL, "ORG_ID": "101", "PRD_DE": period or str(year), "PRD_SE": prd_se,
        "ITM_ID": "T10", "ITM_NM": "인구",
        "C1": region, "C1_NM": REGIONS[region], "C2": sex, "C2_NM": SEXES[sex],
        "C3": edu, "C3_NM": EDUS[edu], "DT": dt, "UNIT_NM": "명", "LST_CHN_DE": "2021-01-01",
    }  # fmt: skip


def pick(param: str, names: dict[str, str]) -> list[str]:
    return list(names) if param == "ALL" else param.split("+")


@pytest.fixture(autouse=True)
def fresh_registry():
    """등록이 다른 테스트로 새지 않게 테스트마다 비운다"""
    token = partial_table._ACTIVE.set(None)
    yield
    partial_table._ACTIVE.reset(token)


@pytest.fixture
def kosis(monkeypatch: pytest.MonkeyPatch) -> dict:
    """KOSIS 응답(고른 칸만)·기준 지역·대기열을 바꿔 끼운다. state["calls"]에 보낸 요청"""
    state = {"calls": [], "queued": [], "metas": 0, "fail": None, "outcome": "too_big"}

    async def call(client, path, params, pause=1.0):
        assert path == collect_all.DATA_PATH and pause == 0
        if state["fail"]:
            raise state["fail"]
        state["calls"].append(params)
        years = range(int(params["startPrdDe"]), int(params["endPrdDe"]) + 1)
        return [
            record(y, r, s, e)
            for y in years
            for r in pick(params["objL1"], REGIONS)
            for s in pick(params["objL2"], SEXES)
            for e in pick(params["objL3"], EDUS)
        ]

    async def table_meta(tbl_id, org_id):
        state["metas"] += 1
        return make_meta()

    async def fetch_now(tbl_id, org_id, trace=None):
        trace["fetch"] = state["outcome"]
        return state["outcome"]

    async def enqueue(tbl_id, reason):
        state["queued"].append((tbl_id, reason))
        return {"id": 1, "status": "queued"}

    monkeypatch.setattr(collect_kosis, "call", call)
    monkeypatch.setattr(table_cache, "table_meta", table_meta)
    monkeypatch.setattr(table_cache, "fetch_now", fetch_now)
    monkeypatch.setattr(table_cache, "_limiter", None)  # asyncio.run마다 새 이벤트 루프
    monkeypatch.setattr(load_queue, "enqueue", enqueue)
    regions = (
        ("00", "전국", "전국", "전국", None, None),
        ("11", "서울특별시", "시도", "서울특별시", None, None),
        ("21", "부산광역시", "시도", "부산광역시", 2018, None),  # 2018년부터 유효
    )
    monkeypatch.setattr(partial_table, "_regions", lambda: regions)
    # 표 이름·출처·지역 이름 (적재 여부와 상관없는 DB 조회)
    monkeypatch.setattr(lookup, "_table", lambda t: {"tbl_nm": make_meta()["tbl_nm"],
                                                     "org_nm": "시험청", "basis": ""})  # fmt: skip
    monkeypatch.setattr(lookup, "_projection", lambda t: False)
    monkeypatch.setattr(d, "is_projection", lambda t: False)
    monkeypatch.setattr(indicators, "find_regions", lambda text: [])
    monkeypatch.setattr(indicators, "kosis_url", lambda t: f"https://kosis.kr/{t}")
    monkeypatch.setattr(indicators, "in_year", lambda reg, year: reg)

    def resolve_region(name, year=None, sigungu=False):
        code = {"서울특별시": "11", "부산광역시": "21", "전국": "00"}[name]
        kind = "전국" if code == "00" else "시도"
        return {"status": "ok", "display": name, "region_code": code, "kind": kind}

    monkeypatch.setattr(indicators, "resolve_region", resolve_region)

    def not_loaded(tbl_id):  # 적재 안 된 표 (raw.stat_load에 없음)
        raise StatsTableMissing(f"{tbl_id} 적재 안 됨", tbl_id)

    monkeypatch.setattr(lookup, "_tbl_key", not_loaded)
    return state


def table(**over) -> partial_table.Table:
    t = partial_table.from_meta(TBL, "101", over.get("meta") or make_meta())
    partial_table.register(t)
    return t


def fill(t: partial_table.Table) -> None:
    """표 전체(연 단위 모든 칸)를 받은 것처럼 채운다 + 월 행 하나"""
    params = {"prdSe": "Y", "itmId": "T10", "objL1": "ALL", "objL2": "ALL", "objL3": "ALL"}
    rows = [record(y, r, s, e) for y in YEARS for r in REGIONS for s in SEXES for e in EDUS]
    rows.append(record(2018, "11", "0", "03", prd_se="M", period="201801"))
    partial_table.add_records(t, params, rows)
    partial_table.done(t, partial_table.Fetch([params], {"T10"}, True))


# ---- 메타 → 칸 목록 · 지역 매핑 -------------------------------------------------------------


@pytest.mark.usefixtures("kosis")
def test_meta_gives_cells_and_region_map() -> None:
    table()
    dims, region_dim, level = d.schema(TBL)
    assert [(x.no, x.name) for x in dims] == [
        (0, "항목"), (1, "행정구역별"), (2, "성별"), (3, "교육정도별"),
    ]  # fmt: skip
    assert dims[3].codes == EDUS and dims[3].total() == "00"
    assert (region_dim, level) == (1, "시도")
    assert lookup._region_dim(TBL) == 1
    assert {c: n for c, (_, n) in lookup._codes(TBL, 2).items()} == SEXES
    assert lookup._dim_labels(TBL, "c3") == tuple(sorted(EDUS.items()))


@pytest.mark.usefixtures("kosis")
def test_table_without_yearly_data_is_unsupported() -> None:
    monthly = [{"PRD_SE": "월", "STRT_PRD_DE": "2015.01", "END_PRD_DE": "2020.12"}]
    with pytest.raises(partial_table.Unsupported):
        partial_table.from_meta(TBL, "101", make_meta(monthly))


@pytest.mark.usefixtures("kosis")
def test_caches_are_not_filled_with_partial_table() -> None:
    """적재 뒤에는 DB 값을 읽어야 하므로 lru 캐시에 칸만 받은 표가 들어가면 안 된다"""
    for cached in lookup.TABLE_CACHES:
        cached.cache_clear()
    t = table()
    fill(t)
    lookup._codes(TBL, 3), lookup._dim_labels(TBL, "c3"), lookup._region_dim(TBL)
    lookup._item_years(TBL, ("T10",))
    assert all(c.cache_info().currsize == 0 for c in lookup.TABLE_CACHES)


# ---- 받을 칸 -------------------------------------------------------------------------------

VALUE_SPEC = {"sources": [{"table": TBL, "level": "시도", "item_id": "T10",
                           "filters": {"c2_id": "0", "c3_id": "03"}}]}  # fmt: skip


@pytest.mark.usefixtures("kosis")
def test_requests_only_the_chosen_cells() -> None:
    t = table()
    fetch = partial_table.requests_for(t, VALUE_SPEC, {"year": 2018})
    assert fetch.full and fetch.items == {"T10"}
    # 지역은 전부, 고른 분류는 그 칸만, 연 단위 기간 전체를 요청 한 번에
    assert fetch.requests == [
        {"prdSe": "Y", "startPrdDe": "2015", "endPrdDe": "2020",
         "itmId": "T10", "objL1": "ALL", "objL2": "0", "objL3": "03"}
    ]  # fmt: skip
    parts = {**VALUE_SPEC, "parts_op": {"col": "c3_id", "codes": ["02", "03"]}}
    assert partial_table.requests_for(t, parts, {}).requests[0]["objL3"] == "02+03"
    # 분류의 칸을 모두 고르면 ALL로 보낸다 (성별 계·남·여)
    src = {**VALUE_SPEC["sources"][0], "filters": {"c3_id": "03"}}
    sexes = (("계", "0"), ("남", "1"), ("여", "2"))
    src["by_sex"] = {s: {"filters": {"c2_id": c}} for s, c in sexes}
    assert partial_table.requests_for(t, {"sources": [src]}, {}).requests[0]["objL2"] == "ALL"


@pytest.mark.usefixtures("kosis")
def test_too_many_cells_asks_only_the_year_then_gives_up(monkeypatch) -> None:
    t = table()
    monkeypatch.setattr(partial_table, "MAX_REQUESTS", 1)
    monkeypatch.setattr(collect_all, "MAX_CELLS", 10)  # 지역 3칸 × 6년 = 18칸 → 기간 전체는 2번
    fetch = partial_table.requests_for(t, VALUE_SPEC, {"year": 2018})
    assert not fetch.full
    assert [(r["startPrdDe"], r["endPrdDe"]) for r in fetch.requests] == [("2018", "2018")]
    # 연도를 말하지 않으면 마지막 해
    assert partial_table.requests_for(t, VALUE_SPEC, {}).requests[0]["startPrdDe"] == "2020"
    monkeypatch.setattr(collect_all, "MAX_CELLS", 2)  # 물은 해 하나도 지역 3칸이라 2번
    assert partial_table.requests_for(t, VALUE_SPEC, {"year": 2018}) is None


@pytest.mark.usefixtures("kosis")
def test_requests_already_sent_are_not_sent_again() -> None:
    t = table()
    fetch = partial_table.requests_for(t, VALUE_SPEC, {})
    partial_table.add_records(t, fetch.requests[0], [])
    assert partial_table.requests_for(t, VALUE_SPEC, {}).requests == []


# ---- DB 조회와 같은 계산 -------------------------------------------------------------------


@pytest.mark.usefixtures("kosis")
def test_sum_follows_lookup_rules() -> None:
    t = table()
    fill(t)
    src = {"table": TBL, "level": "시도", "item_id": "T10"}
    got = lookup._sum(src, ["11", "21"], 2016, 2018, "item_id", {"c2_id": "0", "c3_id": "03"}, None)
    # 부산은 2018년부터 유효한 지역 (지역 매핑의 유효 연도), 월 행은 보지 않는다
    assert got == {(y, "11"): value(y, "11", "0", "03") for y in (2016, 2017, 2018)} | {
        (2018, "21"): value(2018, "21", "0", "03")
    }
    assert lookup._sum(src, ["00"], 2018, 2018, "item_id", {"c2_id": "0", "c3_id": "00"}, None) == {
        (2018, "00"): value(2018, "00", "0", "00")
    }
    # 비공개 칸만 있으면 None (SQL의 count(value) = 0)
    flagged = {"c2_id": "2", "c3_id": "01"}
    t.region_rows = [{**m, "valid_from": None} for m in t.region_rows]
    assert lookup._sum(src, ["21"], 2016, 2016, "item_id", flagged, None) == {(2016, "21"): None}
    # 분류별로 나눠 합하기 (구성비)
    by_edu = lookup._sum(src, ["11"], 2018, 2018, "item_id", {"c2_id": "0"}, None, group_dim="c3")
    assert by_edu == {(2018, "11", e): value(2018, "11", "0", e) for e in EDUS}


@pytest.mark.usefixtures("kosis")
def test_unit_and_years_from_received_rows() -> None:
    t = table()
    assert lookup._item_years(TBL, ("T10",)) == (2015, 2020)  # 받기 전: 메타의 수록 기간
    assert d._unit(TBL, "T10") == (None, False)
    fill(t)
    assert d._unit(TBL, "T10", {3: ["03"]}) == ("명", True)
    assert lookup._item_years(TBL, ("T10",)) == (2015, 2020)


@pytest.mark.usefixtures("kosis")
def test_whole_table_check_is_not_done_on_partial_table(monkeypatch) -> None:
    table()

    def rows(*args):
        raise AssertionError("표 전체 검증을 DB로 하지 않는다")

    monkeypatch.setattr(d, "_part_rows", rows)
    d.CHECKS.clear()
    src = {"table": TBL, "item_id": "T10", "filters": {"c2_id": "0"}}
    assert d.part_within_whole(src, "c3_id", ["03"], "00") == partial_table.CHECK_REASON
    assert d.CHECKS.stats()["size"] == 0  # 적재 뒤에는 검증하도록 남기지 않는다


# ---- 질문 처리 (dynamic_indicator._try) ----------------------------------------------------

COMPARE = "2018년 서울 중학교랑 고등학교 중 어느 쪽 인구가 더 많아?"
PARSED = {"type": "compare", "regions": ["서울특별시"], "year": 2018, "sex": "계",
          "indicator": "교육정도별 인구"}  # fmt: skip


def run_try(question: str, parsed: dict, times: int = 1) -> tuple[list, dict, list]:
    async def go():
        out = []
        for _ in range(times):
            out.append(await d._try(TBL, "101", question, parsed, trace, {}))
        return out

    trace: dict = {"dynamic": []}
    steps = flow.start()
    return asyncio.run(go()), trace, steps


def test_big_table_answers_from_chosen_cells(kosis) -> None:
    [(res, ask)], trace, steps = run_try(COMPARE, PARSED)
    assert ask is None and res["status"] == "ok", trace
    labels = {c["label"]: c["value"] for c in res["result"]["categories"]}
    assert labels == {
        "중학교": value(2018, "11", "0", "02"),
        "고등학교": value(2018, "11", "0", "03"),
    }
    assert res["result"]["tbl_id"] == TBL  # 출처는 그 표
    # 표 전체 적재는 대기열이 맡는다
    assert kosis["queued"] == [(TBL, "question")]
    # 고른 칸만 요청 한 번
    assert len(kosis["calls"]) == 1 and kosis["calls"][0]["objL3"] == "02+03"
    step = trace["dynamic"][-1]
    assert step["fetch"] == "too_big" and step["partial"]["requests"] == 1
    assert "칸만 바로 받음 (요청 1번)" in steps[-1]["detail"]


def test_same_table_again_reuses_received_cells(kosis) -> None:
    results, trace, _ = run_try(COMPARE, PARSED, times=2)
    assert [r[0]["status"] for r in results] == ["ok", "ok"]
    assert kosis["metas"] == 1 and len(kosis["calls"]) == 1
    assert trace["dynamic"][-1]["partial"]["requests"] == 0


def test_ratio_waits_for_whole_table(kosis) -> None:
    parsed = {**PARSED, "type": "value"}
    [(res, _)], trace, _ = run_try("2018년 서울 인구 중 고등학교 비율은?", parsed)
    assert res is None and trace["dynamic"][-1]["skip"] == partial_table.CHECK_REASON
    assert kosis["calls"] == [] and kosis["queued"] == [(TBL, "question")]


def test_too_many_requests_falls_back_to_table_guide(kosis, monkeypatch) -> None:
    monkeypatch.setattr(collect_all, "MAX_CELLS", 1)
    [(res, _)], trace, _ = run_try(COMPARE, PARSED)
    assert res is None and "칸이 많아 칸만 받지 못함" in trace["dynamic"][-1]["skip"]
    assert kosis["calls"] == []


def test_fetch_failure_hides_api_key(kosis) -> None:
    kosis["fail"] = collect_kosis.KosisError("20: 인증 오류 (https://kosis.kr/x?apiKey=SECRET&a=1)")
    [(res, _)], trace, _ = run_try(COMPARE, PARSED)
    step = trace["dynamic"][-1]
    assert res is None and "칸만 받기 실패" in step["skip"]
    assert "SECRET" not in step["partial_error"] and "apiKey=***" in step["partial_error"]


def test_failed_whole_fetch_does_not_try_cells(kosis) -> None:
    kosis["outcome"] = "failed"  # KOSIS 오류면 칸만 받기도 하지 않고 지금처럼 대기열
    [(res, _)], trace, _ = run_try(COMPARE, PARSED)
    assert res is None and trace["dynamic"][-1]["skip"] == "적재 안 됨 (failed, 대기열에 넣음)"
    assert kosis["metas"] == 0 and kosis["calls"] == []


def test_partial_fetch_can_be_turned_off(kosis, monkeypatch) -> None:
    monkeypatch.setattr(table_cache, "PARTIAL_FETCH", False)
    [(res, _)], trace, _ = run_try(COMPARE, PARSED)
    assert res is None and trace["dynamic"][-1]["skip"] == "적재 안 됨 (too_big, 대기열에 넣음)"
