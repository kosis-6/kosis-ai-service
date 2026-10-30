"""통계표 검색: 질문 → 표 하나의 링크 (실전 2 F-04 · F-05 · F-18 · F-06 1차)

  1. 질의 분석 (slots.extract)       질문 → 슬롯
  2. 후보 검색 (keyword_scores)      주제·대상 단어가 표명·경로·분류·항목에 얼마나 있는지.
                                     단어마다 희귀도(IDF) 가중치 ('인구'처럼 흔한 단어는 덜 침)
  3. 조건 검증·재순위 (check)        표(판) 단위로 연도·지역 수준·성별·분류·값·대상을 판정해 가감점.
                                     질문에 없는 조건(null)은 판정하지 않는다
  4. 패밀리 묶기 (fold)              같은 패밀리는 한 자리만 차지, 패밀리 점수 = 소속 표 최고 점수
  5. 멤버·판 선택 (choose)           대상·성별로 멤버, 연도로 판을 고른다 (검색 규칙 2.4)
  6. 상태 판단                       ok / partial_match / clarify / ambiguous / out_of_scope
                                     / not_statistics. 대상이 빠지면 clarify (되묻기, 규칙 2.3 가안)

표 메타데이터(stat_tables + table_families, 2천여 행)는 처음 한 번 읽어 메모리에 둔다.
되묻기 다음 턴(답 맞추기·세션)은 dialog.py가 맡는다. 임베딩 검색(pgvector)은 아직 없다.

실행: python -m app.services.search "2024년 부산 시군구별 혼인 귀화자 수"
"""

import asyncio
import json
import math
import re
import sys
import time
from typing import Any

from sqlalchemy import text

from app.services import slots as slot_parser

# 가중치·기준값은 개발셋으로 조정할 값 (기능 정의서 F-05, F-06 미결)
FIELD_WEIGHTS = {"name": 1.0, "path": 0.6, "meta": 0.4}
NAME_WEIGHT = 0.3
PASS_BONUS = 0.08
FAIL_PENALTY = 0.15
MIN_SCORE = 0.45  # 1순위 패밀리 점수가 이보다 낮으면 out_of_scope
AMBIGUOUS_GAP = 0.03  # 1·2순위 패밀리 점수 차가 이보다 작으면 ambiguous
TOP_FAMILIES = 4

STOPWORDS = {
    "수", "통계", "통계표", "자료", "데이터", "현황", "관련", "표", "정보", "찾아줘", "알려줘",
    "보여줘", "몇", "명", "얼마", "얼마나", "몇명", "추이", "변화", "있어", "있나요",
}  # fmt: skip
# '취득한'·'취득자' → '취득', '국적을' → '국적'. 조사·어미 한 글자만 떼어 본다
SUFFIXES = ("한", "된", "을", "를", "의", "은", "는", "이", "가", "에", "별", "인", "자")
MIGRATION = re.compile(r"이동|전입|전출|이주|이사")

TABLES_SQL = """
SELECT s.tbl_id, s.org_id, s.table_name, s.survey_name, s.organization, s.period, s.path_texts,
       s.classifications, s.items, s.start_year, s.end_year, s.region_levels,
       s.prev_region_levels, s.sex_coverage, s.has_total, s.url,
       f.family_id, f.family_name, f.target, f.sex, f.edition_order, f.edition, f.renamed_from
FROM stat_tables s JOIN table_families f USING (tbl_id)
"""

_tables: list[dict[str, Any]] | None = None


def norm(value: str) -> str:
    """띄어쓰기·기호를 지운다 ('1인 가구' = '1인가구')"""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", value or "")


def prepare(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """검색에 쓸 정규화 문자열을 붙인다"""
    tables = []
    for r in rows:
        t = dict(r)
        classes = t["classifications"] or []
        folders = [p.rsplit(">", 1)[0] for p in t["path_texts"] or []]
        t["name_n"] = norm(t["table_name"])
        t["path_n"] = norm(" ".join([t["survey_name"], *folders]))
        t["class_n"] = [norm(c["name"]) for c in classes]
        t["meta_n"] = norm(
            " ".join(
                [c["name"] for c in classes]
                + [v for c in classes for v in c.get("sample_values") or [] if v]
                + [i["name"] for i in t["items"] or []]
            )
        )
        tables.append(t)
    return tables


async def load_tables() -> list[dict[str, Any]]:
    global _tables
    if _tables is None:
        from app.core.db import engine

        async with engine.connect() as conn:
            rows = (await conn.execute(text(TABLES_SQL))).mappings().all()
        _tables = prepare([dict(r) for r in rows])
    return _tables


# ---- 2. 후보 검색 ---------------------------------------------------------------


def query_tokens(slots: dict[str, Any]) -> list[dict[str, Any]]:
    """주제·대상 → 단어별 변형 목록 ('취득한' → ['취득한', '취득']).

    value: 슬롯 items에 든 단어('미혼 인구'의 '미혼'). 분류 값이라 표명에 없어도 되므로
    어느 필드에 있든 같은 점수를 준다.
    """
    words = re.findall(r"[0-9A-Za-z가-힣]+", f"{slots['topic']} {slots.get('target') or ''}")
    values = norm(" ".join(slots["conditions"]["items"]))
    tokens = []
    for w in dict.fromkeys(words):
        if w in STOPWORDS:
            continue
        variants = [w]
        if len(w) >= 3 and w.endswith(SUFFIXES):
            variants.append(w[:-1])
        tokens.append({"variants": variants, "value": bool(values) and w in values})
    return tokens


def _found(variants: list[str], field: str) -> bool:
    return any(v in field for v in variants)


def keyword_scores(tables: list[dict[str, Any]], tokens: list[dict[str, Any]]) -> list[float]:
    """표마다 0~1 점수: 단어 희귀도(IDF) 가중 평균으로 어느 필드에 있는지 본다"""
    n = len(tables)
    idf = []
    for token in tokens:
        variants = token["variants"]
        df = sum(
            _found(variants, t["name_n"])
            or _found(variants, t["path_n"])
            or _found(variants, t["meta_n"])
            for t in tables
        )
        # 어느 표에도 없는 단어('아파트 매매 가격'의 '매매')는 가장 희귀한 단어로 쳐서 점수를 깎는다
        idf.append(math.log((n + 1) / (df + 1)) if df else math.log(n + 1))
    total = sum(idf)
    if total == 0:
        return [0.0] * n
    scores = []
    for t in tables:
        s = 0.0
        for token, w in zip(tokens, idf, strict=True):
            s += w * max(
                (
                    1.0 if token["value"] else FIELD_WEIGHTS[f]
                    for f in ("name", "path", "meta")
                    if _found(token["variants"], t[f"{f}_n"])
                ),
                default=0.0,
            )
        scores.append(s / total)
    return scores


# ---- 3. 조건 검증 ---------------------------------------------------------------


def check(t: dict[str, Any], slots: dict[str, Any]) -> dict[str, bool]:
    """판(표) 단위 조건 판정. 질문에 없는 조건은 결과에 넣지 않는다"""
    c = slots["conditions"]
    out: dict[str, bool] = {}
    if c["year"] is not None:
        last = c["year_end"] or c["year"]
        out["year"] = t["start_year"] <= c["year"] and last <= t["end_year"]
    if c["region_level"]:
        levels = set(t["region_levels"])
        if MIGRATION.search(slots["topic"]):
            levels |= set(t["prev_region_levels"])
        out["region_level"] = c["region_level"] in levels
    sex = c["sex"]
    if sex in ("남", "여"):
        own = "male_only" if sex == "남" else "female_only"
        out["sex"] = t["sex_coverage"] in ("both", own) or t["sex"] == sex
    elif sex == "전체" and t["sex_coverage"] != "none":
        out["sex"] = t["has_total"] is not False
    for cls in c["classes"]:
        if cls == "성별":
            ok = t["sex_coverage"] == "both"
        elif cls == "가구주 성별":
            ok = any("가구주" in n and "성" in n for n in t["class_n"])
        else:
            key = norm(cls).removesuffix("별")
            ok = any(key in n for n in t["class_n"]) or key in t["name_n"]
        out[f"class:{cls}"] = ok
    for item in c["items"]:
        key = norm(item)
        out[f"item:{item}"] = key in t["meta_n"] or key in t["name_n"]
    # '전체'는 멤버 선택값이지 표가 갖춰야 할 조건이 아니다
    if slots.get("target") and slots["target"] != "전체":
        key = norm(slots["target"])
        out["target"] = key in t["name_n"] or key in t["meta_n"] or key in norm(t["target"] or "")
    return out


def bigrams(value: str) -> set[str]:
    return {value[i : i + 2] for i in range(len(value) - 1)}


def name_similarity(query: str, name: str) -> float:
    """주제와 표명의 글자 2개 단위 겹침 (Dice). 같은 단어를 가진 표 중 이름이 가까운 표를 앞으로"""
    a, b = bigrams(query), bigrams(name)
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


def rank(tables: list[dict[str, Any]], slots: dict[str, Any]) -> list[dict[str, Any]]:
    tokens = query_tokens(slots)
    query = norm(f"{slots['topic']} {slots.get('target') or ''}")
    ranked = []
    for t, kw in zip(tables, keyword_scores(tables, tokens), strict=True):
        if kw == 0:
            continue
        checks = check(t, slots)
        passed = sum(checks.values())
        score = (
            kw
            + NAME_WEIGHT * name_similarity(query, t["name_n"])
            + PASS_BONUS * passed
            - FAIL_PENALTY * (len(checks) - passed)
        )
        ranked.append({"table": t, "keyword": kw, "checks": checks, "score": score})
    ranked.sort(key=lambda r: (-r["score"], r["table"]["edition_order"], r["table"]["tbl_id"]))
    return ranked


# ---- 4·5. 패밀리 묶기, 멤버·판 선택 ----------------------------------------------


def fold(ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 패밀리는 최고 점수 표 하나로 접는다"""
    seen: dict[str, dict[str, Any]] = {}
    for r in ranked:
        seen.setdefault(r["table"]["family_id"], r)
    return list(seen.values())


def family_axes(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    def options(key: str) -> list[str]:
        values = {r[key] for r in rows if r[key] is not None}
        return sorted(values, key=lambda v: (v != "전체" and v != "계", v))

    axes = {"target": options("target"), "sex": options("sex")}
    # 최신 연도를 담은 판 → 기간이 긴 판 순 (규칙 2.4, '2006~2015 / 2007~2015 / 2015')
    editions = sorted({(-r["end_year"], r["start_year"], r["edition"]) for r in rows})
    axes["edition"] = list(dict.fromkeys(e for _, _, e in editions))
    return {k: v for k, v in axes.items() if len(v) > 1}


def choose(
    family: list[dict[str, Any]], slots: dict[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """패밀리 안에서 표 하나 → (표, 빠진 축, 연도 메모)

    되묻기 답이나 화면 버튼으로 고른 값은 slots의 target('전체' 포함)·edition·tbl_id로 들어온다.
    """
    c = slots["conditions"]
    picked = [r for r in family if r["tbl_id"] == slots.get("tbl_id")]
    if picked:  # 화면에서 표를 직접 고른 경우
        return picked[0], [], []
    axes = family_axes(family)
    missing = []
    rows = family

    if "target" in axes:
        # 대상 슬롯이 패밀리명이어도('한국국적 취득자') 질문 속 멤버 이름('혼인 귀화자')으로 고른다
        said = norm(f"{slots.get('question', '')} {slots.get('target') or ''}")
        match = [r for r in rows if r["target"] != "전체" and norm(r["target"]) in said]
        if slots.get("target") == "전체":
            match = [r for r in rows if r["target"] == "전체"]
        if not match:
            missing.append("target")
            match = [r for r in rows if r["target"] == "전체"] or rows
        rows = match
    if "sex" in axes:
        want = {"남": "남", "여": "여", "전체": "계"}.get(c["sex"] or "")
        match = [r for r in rows if r["sex"] == want]
        if not match:
            if c["sex"] is None:
                missing.append("sex")
            match = [r for r in rows if r["sex"] == "계"] or rows
        rows = match
    rows = [r for r in rows if r["edition"] == slots.get("edition")] or rows

    notes = []
    year = c["year"]
    if year is None:
        # 가장 최신 연도를 담은 판 → 그중 기간이 긴 판 (edition_order 1)
        best = min(rows, key=lambda r: (r["edition_order"], r["tbl_id"]))
    else:
        last = c["year_end"] or year
        covering = [r for r in rows if r["start_year"] <= year and last <= r["end_year"]]
        if covering:
            best = max(covering, key=lambda r: (r["end_year"] - r["start_year"], r["end_year"]))
        else:
            best = min(
                rows,
                key=lambda r: (
                    max(r["start_year"] - year, year - r["end_year"], 0),
                    r["edition_order"],
                ),
            )
            notes.append(
                f"{year}년을 담은 판이 없어 가장 가까운 {best['edition']}년 판을 골랐습니다"
            )
    return best, missing, notes


# ---- 6. 상태 판단 ---------------------------------------------------------------


def _brief(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "tbl_id": t["tbl_id"],
        "table_name": t["table_name"],
        "survey_name": t["survey_name"],
        "edition": t["edition"],
        "url": t["url"],
    }


def find(
    tables: list[dict[str, Any]], slots: dict[str, Any], allow_clarify: bool = True
) -> dict[str, Any]:
    """슬롯 → 검색 결과 (DB·HCX 없이 동작해서 테스트·실험에 쓴다)"""
    result: dict[str, Any] = {"status": None, "reason": None, "slots": slots, "table": None}
    if slots["query_type"] == "not_statistics":
        return {**result, "status": "not_statistics", "reason": "통계표를 찾는 질문이 아닙니다"}

    ranked = rank(tables, slots)
    families = fold(ranked)
    result["candidates"] = [
        {**_brief(r["table"]), "score": round(r["score"], 3), "checks": r["checks"]}
        for r in ranked[:10]
    ]
    if not families or families[0]["score"] < MIN_SCORE:
        return {
            **result,
            "status": "out_of_scope",
            "reason": "질문에 맞는 통계표를 찾지 못했습니다",
        }

    top = families[0]
    others = [
        {
            "family_id": f["table"]["family_id"],
            "family_name": f["table"]["family_name"],
            "score": round(f["score"], 3),
            "checks": f["checks"],
            **_brief(f["table"]),
        }
        for f in families[1:TOP_FAMILIES]
    ]
    result["other_families"] = others
    if len(families) > 1 and top["score"] - families[1]["score"] < AMBIGUOUS_GAP:
        close = [o for o in others if top["score"] - o["score"] < AMBIGUOUS_GAP]
        result["options"] = [
            {"family_id": top["table"]["family_id"], "family_name": top["table"]["family_name"],
             "score": round(top["score"], 3), **_brief(top["table"])},
            *close,
        ]  # fmt: skip
        return {**result, "status": "ambiguous", "reason": "비슷한 통계표 묶음이 여럿입니다"}

    return finish(result, tables, top["table"]["family_id"], top["score"], allow_clarify)


def resolve(
    tables: list[dict[str, Any]],
    slots: dict[str, Any],
    family_id: str,
    allow_clarify: bool = True,
) -> dict[str, Any]:
    """패밀리가 정해진 뒤(되묻기 답) 검색 없이 멤버·판만 고른다"""
    result = {"status": None, "reason": None, "slots": slots, "table": None, "candidates": []}
    return finish(result, tables, family_id, None, allow_clarify)


def clarify_axes(
    family: list[dict[str, Any]], slots: dict[str, Any], missing: list[str]
) -> list[dict[str, Any]]:
    """되묻기 축 (규칙 2.3 가안). 기본값이 없는 대상이 빠졌을 때만 묻고,
    기본값이 있는 성별·연도는 같은 메시지에 함께 넣는다"""
    if "target" not in missing:
        return []
    axes = family_axes(family)
    asked = [{"axis": "target", "options": axes["target"]}]
    if "sex" in missing:
        asked.append({"axis": "sex", "options": ["전체" if v == "계" else v for v in axes["sex"]]})
    no_year = slots["conditions"]["year"] is None and not slots.get("edition")
    if "edition" in axes and no_year:
        asked.append({"axis": "edition", "options": axes["edition"]})
    return asked


def finish(
    result: dict[str, Any],
    tables: list[dict[str, Any]],
    family_id: str,
    score: float | None,
    allow_clarify: bool,
) -> dict[str, Any]:
    """패밀리 확정 후: 멤버·판 선택 → 되묻기 또는 표 하나"""
    slots = result["slots"]
    family = [t for t in tables if t["family_id"] == family_id]
    table, missing, notes = choose(family, slots)
    info = {
        "family_id": family_id,
        "family_name": table["family_name"],
        "survey_name": table["survey_name"],
        "organization": table["organization"],
        "axes": family_axes(family),
    }
    asked = clarify_axes(family, slots, missing) if allow_clarify else []
    if asked:
        return {
            **result,
            "status": "clarify",
            "score": score,
            "family": info,
            "clarify_axes": asked,
            "missing_axes": missing,
            "reason": "패밀리 안에서 표를 고를 조건이 부족합니다",
        }
    checks = check(table, slots)
    failed = [k for k, ok in checks.items() if not ok]
    result.update(
        {
            "status": "partial_match" if failed or notes else "ok",
            "score": score,
            "reason": "; ".join([*notes, *(f"조건 불충족: {k}" for k in failed)]) or None,
            "notes": notes,
            "failed": failed,
            "table": {**_brief(table), "period": table["period"], "checks": checks},
            "family": info,
            "missing_axes": missing,
            "other_editions": [
                _brief(t)
                for t in sorted(family, key=lambda t: -t["end_year"])
                if t["tbl_id"] != table["tbl_id"]
                and t["target"] == table["target"]
                and t["sex"] == table["sex"]
            ],
        }
    )
    return result


async def search(
    question: str, trace: dict[str, Any] | None = None, allow_clarify: bool = True
) -> dict[str, Any]:
    """trace(dict)를 주면 HCX 원본 출력과 단계별 시간(ms)을 담는다 (요청 로그용)"""
    trace = {} if trace is None else trace
    start = time.perf_counter()
    slots = await slot_parser.extract(question, trace)
    parsed_at = time.perf_counter()
    result = find(await load_tables(), slots, allow_clarify)
    trace["timings"] = {
        "slots_ms": round((parsed_at - start) * 1000),
        "search_ms": round((time.perf_counter() - parsed_at) * 1000),
    }
    return result


def _print(result: dict[str, Any]) -> None:
    keep = {k: v for k, v in result.items() if k not in ("candidates", "slots")}
    print(json.dumps(keep, ensure_ascii=False, indent=2))
    print("\n후보 상위:")
    for c in result.get("candidates", [])[:5]:
        print(f"  {c['score']:.3f} {c['table_name']} ({c['edition']}) {c['checks']}")


if __name__ == "__main__":
    _print(asyncio.run(search(" ".join(sys.argv[1:]) or "2024년 부산 시군구별 혼인 귀화자 수")))
