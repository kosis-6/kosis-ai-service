import asyncio
import json
import logging
from pathlib import Path

from sqlalchemy import text

from app.core.db import engine
from app.core.stats_db import rows as stats_rows
from app.services import dynamic_indicator, indicators, lookup, plan_cache

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "measure" / "dynamic_cache_eval.json"
TABLE = "DT_1PL1502"
PROJECTION_TABLE = "DT_1BZ0506"
REGIONS = ["서울", "부산", "경기", "제주특별자치도", "서울 강남구", "부산 해운대구", "수원시"]
YEARS = [2015, 2020, 2025]
SEX_ITEM = {"계": "T00", "남": "T10", "여": "T20"}
TERMS = {"value": "1인 가구 수", "ratio": "1인 가구 중 여성 비율", "future": "1인 가구"}


def truth(region: str, year: int, item: str) -> float | None:
    reg = indicators.resolve_region(region, year, sigungu=True)
    items, ages = lookup._codes(TABLE, 0), lookup._codes(TABLE, 2)
    r = stats_rows(
        "select sum(v.value)::float8 as v from stat_value v"
        " join stat_code k on k.code_key = v.c1"
        " join region_code_map m on m.tbl_id = ? and m.src_dim = 1 and m.src_code = k.code"
        "   and m.region_code = ?"
        "   and (m.valid_from is null or v.period >= m.valid_from)"
        "   and (m.valid_to is null or v.period <= m.valid_to)"
        " where v.tbl_key = ? and v.item_key = ? and v.c2 = ? and v.period = ? and v.prd_se = 'A'",
        [
            TABLE,
            reg["region_code"],
            lookup._tbl_key(TABLE),
            items[item][0],
            ages["000"][0],
            year,
        ],
    )
    return r[0]["v"] if r else None


def parsed(term: str, region: str, year: int, sex: str = "계") -> dict:
    return {"type": "value", "indicator": term, "term": term, "regions": [region],
            "year": year, "sex": sex}  # fmt: skip


def value_of(res: dict | None) -> float | None:
    if not res or res.get("status") != "ok":
        return None
    return (res.get("result") or {}).get("value")


async def seed(term: str, tbl_id: str, question: str, p: dict) -> None:
    spec, why = dynamic_indicator.build(tbl_id, question, p)
    assert spec, f"{tbl_id}: {why}"
    key, _ = plan_cache.key_of(p)
    await plan_cache.save(key, term, tbl_id, {}, spec)


async def run() -> dict:
    keys = []
    for term in TERMS.values():
        found = plan_cache.key_of({"term": term})
        keys.append(found[0])
    async with engine.begin() as conn:
        await conn.execute(text("delete from app.dynamic_plans where key = any(:k)"), {"k": keys})
    plan_cache.CACHE.clear()
    try:
        await seed(
            TERMS["value"], TABLE, "2020년 서울 1인 가구 수", parsed(TERMS["value"], "서울", 2020)
        )
        q = "2020년 서울 1인 가구 중 여성 비율"
        await seed(TERMS["ratio"], TABLE, q, parsed(TERMS["ratio"], "서울", 2020, "여"))
        q = "2040년 서울 1인 가구"
        await seed(TERMS["future"], PROJECTION_TABLE, q, parsed(TERMS["future"], "서울", 2040))

        cases = []
        for region in REGIONS:
            for year in YEARS:
                for sex, item in SEX_ITEM.items():
                    p = parsed(TERMS["value"], region, year, sex)
                    words = {"계": "", "남": "남자 ", "여": "여자 "}[sex]
                    question = f"{year}년 {region} {words}1인 가구 수"
                    got = value_of(await dynamic_indicator.cached_answer(question, p, {}))
                    cases.append(
                        {"kind": "값", "region": region, "year": year, "sex": sex,
                         "got": got, "want": truth(region, year, item)}
                    )  # fmt: skip
                p = parsed(TERMS["ratio"], region, year, "여")
                question = f"{year}년 {region} 1인 가구 중 여성 비율"
                got = value_of(await dynamic_indicator.cached_answer(question, p, {}))
                num, den = truth(region, year, "T20"), truth(region, year, "T00")
                want = round(num / den * 100, 2) if num is not None and den else None
                cases.append(
                    {"kind": "여성 비율", "region": region, "year": year, "sex": "여",
                     "got": got, "want": want}
                )  # fmt: skip

        future = await dynamic_indicator.cached_answer(
            "2040년 서울 1인 가구 수", parsed(TERMS["value"], "서울", 2040), {}
        )
        past = await dynamic_indicator.cached_answer(
            "2020년 서울 1인 가구", parsed(TERMS["future"], "서울", 2020), {}
        )
        projected = await dynamic_indicator.cached_answer(
            "2045년 부산 1인 가구", parsed(TERMS["future"], "부산", 2045), {}
        )
    finally:
        async with engine.begin() as conn:
            await conn.execute(
                text("delete from app.dynamic_plans where key = any(:k)"), {"k": keys}
            )
        plan_cache.CACHE.clear()

    def same(c):
        if c["want"] is None:
            return c["got"] is None
        return c["got"] is not None and abs(c["got"] - c["want"]) <= (
            0.01 if c["kind"] != "값" else 0
        )

    checks = {
        "실적 표 → 2040년": {
            "status": (future or {}).get("status"),
            "ok": value_of(future) is None,
        },
        "추계 표 → 2020년": {"status": (past or {}).get("status"), "ok": past is None},
        "추계 표 → 2045년": {
            "status": (projected or {}).get("status"),
            "value": value_of(projected),
            "ok": value_of(projected) is not None,
        },
    }
    wrong = [c for c in cases if not same(c)]
    return {
        "cases": len(cases),
        "correct": len(cases) - len(wrong),
        "wrong": wrong,
        "checks": checks,
    }


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    result = asyncio.run(run())
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f"지역·연도·성별 바꿔 재사용: {result['correct']}/{result['cases']} 정답")
    for c in result["wrong"][:10]:
        print("  틀림:", c)
    for name, c in result["checks"].items():
        print(f"{name}: {'통과' if c['ok'] else '실패'} {c}")


if __name__ == "__main__":
    main()
