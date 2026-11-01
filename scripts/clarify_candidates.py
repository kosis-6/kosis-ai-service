"""되묻기 후보 찾기: 챗봇이 기본값으로 답할 수 없어 되물어야 하는 표 묶음 (검색 규칙 2.3)

되묻기는 '기본값이 없을 때만' 한다. 기본값 = 대상 '전체', 성별 '계', 전국 표, 수식어 없는 표.
기본값이 없는 경우를 세 종류로 찾는다.

  A 대상으로 나뉜 묶음에 '전체' 표가 없음     '차별 받은 경험 - 병원 / - 식당 / …'
  B 지역으로 나뉜 묶음에 전국 표가 없음        '통근통학 인구(강원) / (대구,경북) / …'
  C 한 단어만 바뀐 형제 표에 기본 표가 없음    '평균초혼연령 / 평균재혼연령' (평균혼인연령 표 없음)
                                               '1년 전 거주지별 / 5년 전 거주지별 이동 인구'

묶음은 scripts/family_check.py와 같이 현재 build_families 결과 + 미구현 규칙(R1~R3)으로 만든다.
C는 같은 조사에서 표명이 양쪽에 한 단어씩만 다른 쌍 중, 공통 부분만 가진 표가 없는 경우다.

입력: data/catalog/population_table_catalog_with_meta.jsonl
출력: eval/results/clarify_candidates_YYYYMMDD.md
실행: python -m scripts.clarify_candidates  (family_check를 먼저 실행해 묶음 CSV가 있어야 함)
"""

import csv
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import scripts.family_check as fc
from pipeline.build_families import build as build_families
from pipeline.build_families import edition_name
from pipeline.load_catalog import read_catalog
from scripts.confusable_tables import name_tokens

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"


def latest(pattern: str) -> Path:
    files = sorted(RESULTS.glob(pattern))
    if not files:
        raise SystemExit(f"{pattern} 없음: 먼저 해당 스크립트를 실행하세요")
    return files[-1]


def member_name(name: str) -> str:
    return re.sub(r"\((계|남|여)\)", "", edition_name(name)).strip()


def final_groups(tables: list[dict[str, Any]]) -> list[list[str]]:
    uf = fc.UnionFind([t["tbl_id"] for t in tables])
    fam: dict[str, list[str]] = defaultdict(list)
    for r in build_families(tables):
        fam[r["family_id"]].append(r["tbl_id"])
    for ids in fam.values():
        for x in ids[1:]:
            uf.union(ids[0], x)
    with open(latest("family_check_groups_*.csv"), encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            uf.union(r["tbl_a"], r["tbl_b"])
    return list(uf.groups().values())


def no_default_groups(
    tables: list[dict[str, Any]], by: dict[str, dict[str, Any]]
) -> list[tuple[str, list[str], list[str]]]:
    """(종류, 표 ID들, 멤버 이름들)"""
    out = []
    for ids in final_groups(tables):
        names = sorted({member_name(by[i]["table_name"]) for i in ids})
        if len({fc.loose_key(n) for n in names}) < 2:
            continue  # 판·성별만 다른 묶음은 기본값(최신판·계)이 있다
        regional = [n for n in names if fc.SIDO_RE.search(n)]
        if regional:
            if len(regional) == len(names):
                out.append(("B 지역", ids, names))
            continue
        suffixes = {n.rpartition("-")[2].strip() if "-" in n else "" for n in names}
        if "" not in suffixes and "전체" not in suffixes:
            out.append(("A 대상", ids, names))
    return out


# 성별 멤버·지역 단위 표기는 패밀리 선택과 지역 조건이 처리한다
SKIP = {"계", "남", "여", "전국", "시도", "시군구", "읍면동", "행정구역"}


def axis_words(tables: list[dict[str, Any]]) -> set[str]:
    """분류 이름으로 쓰이는 단어 ('연령', '산업', '혼인상태' …). 이런 단어끼리 바뀐 쌍은
    사용자가 원하는 기준을 말하므로 되묻기 대상이 아니다"""
    words: set[str] = set()
    for t in tables:
        for c in t["classifications"]:
            words |= name_tokens(c["name"])
    return words


def sibling_swaps(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 조사에서 표명이 양쪽에 한 단어씩만 다르고, 공통 부분만 가진 기본 표가 없는 쌍.
    유사도와 상관없이 표명 단어로 직접 찾는다 ('평균초혼연령' / '평균재혼연령')"""
    axes = axis_words(tables)
    shapes: dict[str, set[frozenset[str]]] = defaultdict(set)
    buckets: dict[tuple[str, frozenset[str]], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for t in tables:
        tokens = frozenset(name_tokens(t["table_name"]))
        shapes[t["survey_name"]].add(tokens)
        for w in tokens:
            buckets[(t["survey_name"], tokens - {w})].append((w, t))
    out, seen = [], set()
    for (survey, common), entries in buckets.items():
        if len(common) == 0 or common in shapes[survey]:
            continue
        for i, (wa, a) in enumerate(entries):
            for wb, b in entries[i + 1 :]:
                if wa == wb or (wa in axes and wb in axes) or {wa, wb} & SKIP:
                    continue
                key = tuple(sorted([a["tbl_id"], b["tbl_id"]]))
                if key in seen:
                    continue
                seen.add(key)
                out.append({"swap": tuple(sorted([wa, wb])), "survey_a": survey,
                            "tbl_a": a["tbl_id"], "name_a": a["table_name"],
                            "tbl_b": b["tbl_id"], "name_b": b["table_name"]})  # fmt: skip
    return out


def main() -> None:
    tables = read_catalog()
    by = {t["tbl_id"]: t for t in tables}
    groups = no_default_groups(tables, by)
    swaps = sibling_swaps(tables)
    swap_count = Counter(s["swap"] for s in swaps)
    swap_example = {}
    for s in swaps:
        swap_example.setdefault(s["swap"], s)

    lines = [
        f"# 되묻기 후보 ({date.today():%Y-%m-%d})",
        "",
        "챗봇은 기본값(대상 '전체', 성별 '계', 전국 표, 수식어 없는 표, 최신판)이 있으면 그걸로 답하고,"
        " **기본값이 없을 때만** 되묻는다. 아래는 카탈로그에서 기본값이 없는 경우를 찾은 것이다.",
        "",
        f"## A. 대상으로 나뉘었는데 '전체' 표가 없음 ({sum(g[0] == 'A 대상' for g in groups)}묶음)",
        "",
        "| 묶음 | 표 수 | 선택지(멤버) |",
        "| --- | --- | --- |",
    ]
    for kind, ids, names in groups:
        if kind != "A 대상":
            continue
        prefix = names[0].rpartition("-")[0].strip()
        opts = [n.rpartition("-")[2].strip() for n in names]
        lines.append(f"| {prefix} | {len(ids)} | {' / '.join(opts)} |")
    lines += [
        "",
        f"## B. 지역으로 나뉘었는데 전국 표가 없음 ({sum(g[0] == 'B 지역' for g in groups)}묶음)",
        "",
        "| 대표 표 | 표 수 | 선택지 예 |",
        "| --- | --- | --- |",
    ]
    for kind, ids, names in groups:
        if kind != "B 지역":
            continue
        regions = [m.group(0) for n in names for m in [fc.SIDO_RE.search(n)] if m]
        lines.append(f"| {names[0]} | {len(ids)} | {' / '.join(dict.fromkeys(regions))} |")
    lines += [
        "",
        "## C. 한 단어만 바뀐 형제 표인데 기본 표가 없음",
        "",
        "쌍이 많은 순. '연령/혼인상태', '산업/직업'처럼 **나누는 기준**(분류 이름)끼리 바뀐 쌍과"
        " 성별·지역 단위는 뺐다. 사용자가 원하는 기준을 말하거나 다른 규칙이 처리하기 때문이다."
        " 남은 것 중 사용자가 보통 말하지 않는 **개념 변형**(초혼/재혼, 1년 전/5년 전, 60세/65세,"
        " 주거전용/영업겸용 등)이 되묻기 후보이고, 실제로 되물을지는 사람이 고른다."
        " (판마다 표기만 다른 '14세이상/15세이상', '부인/부인수' 같은 쌍도 섞여 있다)",
        "",
        "| 바뀌는 단어 | 쌍 수 | 예 (표 A / 표 B) |",
        "| --- | --- | --- |",
    ]
    for swap, n in swap_count.most_common(40):
        s = swap_example[swap]
        lines.append(
            f"| {swap[0]} / {swap[1]} | {n} | {s['name_a']} ({s['tbl_a']}) /"
            f" {s['name_b']} ({s['tbl_b']}) |"
        )
    md = "\n".join(lines) + "\n"
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"clarify_candidates_{date.today():%Y%m%d}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
