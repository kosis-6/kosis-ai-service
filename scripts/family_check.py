"""패밀리 묶기 검증: 현재 build_families 결과와 검색 규칙 1장의 미구현 규칙을 더했을 때 비교

현재 코드(pipeline/build_families.py)
  판    같은 조사 + name_key(기호·띄어쓰기만 지운 표명)가 같은 표, '_A' 후속판
  멤버  표명 끝 (계)/(남)/(여)와 그 앞 '-대상'만

검색 규칙 1장에 있는데 코드에 없는 규칙 (차례로 더해 본다)
  R1 표기 변형   구분자 + '및'·'의'·'별'만 다른 표명은 같은 판 (규칙 1.1 ②)
                 '성, 연령 및 직업별 취업인구' = '성별/연령별/직업별 취업인구'
  R3 지역 분할   같은 조사 + 같은 폴더 + 같은 수록 기간 + 같은 분류명·항목 구성인데 이름이 다른 표
                 (규칙 1.2 ①). 이름 차이가 시도명뿐이면 '지역 분할', 아니면 '확인 필요'
  R2 대상 분할   같은 조사 + 같은 분류명 구성 + 표명의 '-' 앞부분이 같은 표 (규칙 1.2 ②).
                 '-' 뒤가 지역 단위(시도·시군구·읍면동)면 다른 표

입력: data/catalog/population_table_catalog_with_meta.jsonl (DB 불필요)
출력: eval/results/family_check_YYYYMMDD.md, family_check_groups_YYYYMMDD.csv
실행: python -m scripts.family_check
"""

import csv
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.build_families import build as build_families
from pipeline.build_families import edition_name
from pipeline.load_catalog import read_catalog

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"

SIDO = (
    "서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|세종특별자치시|"
    "경기도|강원도|강원특별자치도|충청북도|충청남도|전라북도|전북특별자치도|전라남도|경상북도|"
    "경상남도|제주특별자치도|제주도|서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|"
    "전북|전남|경북|경남|제주"
)
SIDO_RE = re.compile(SIDO)
REGION_SUFFIX = re.compile(r"^\s*(시도|시군구|읍면동)\s*$")


class UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {x: x for x in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[ra] = rb
        return True

    def count(self) -> int:
        return len({self.find(x) for x in self.parent})

    def groups(self) -> dict[str, list[str]]:
        g: dict[str, list[str]] = defaultdict(list)
        for x in self.parent:
            g[self.find(x)].append(x)
        return g


def loose_key(name: str) -> str:
    """구분자·'및'·'의'·'별'까지 지운 비교용 표명 (규칙 1.1 ②)"""
    n = edition_name(name)
    n = re.sub(r"\(\s*(계|남|여)\s*\)", "", n)  # 성별 멤버 표기는 멤버 축이라 남긴 채 비교하지 않음
    n = re.sub(r"및|의|별", "", n)
    return re.sub(r"[^0-9A-Za-z가-힣]", "", n)


def sex_tag(name: str) -> str:
    m = re.search(r"\((계|남|여)\)", name)
    return m.group(1) if m else ""


def folder(t: dict[str, Any]) -> str:
    return (t.get("path_texts") or [""])[0].rsplit(" > ", 1)[0]


def class_names(t: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(c["name"] for c in t["classifications"]))


def item_names(t: dict[str, Any]) -> tuple[str, ...]:
    return tuple(sorted(i["name"] for i in t["items"]))


def only_region_differs(names: list[str]) -> bool:
    """시도명(괄호 속 '(서울, 인천, 경기)' 포함)을 지우면 표명이 같아지는가"""
    stripped = {loose_key(SIDO_RE.sub("", n)) for n in names}
    return len(stripped) == 1 and all(SIDO_RE.search(n) for n in names)


def main() -> None:
    tables = read_catalog()
    by_id = {t["tbl_id"]: t for t in tables}
    rows = build_families(tables)
    fam_of = {r["tbl_id"]: r["family_id"] for r in rows}

    uf = UnionFind([t["tbl_id"] for t in tables])
    for members in _group(fam_of).values():
        for x in members[1:]:
            uf.union(members[0], x)
    steps = [("현재 코드", uf.count(), 0, [])]
    log: list[dict[str, Any]] = []

    # R1 표기 변형
    merged, examples = 0, []
    buckets: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for t in tables:
        buckets[(t["survey_name"], loose_key(t["table_name"]), sex_tag(t["table_name"]))].append(
            t["tbl_id"]
        )
    for ids in buckets.values():
        for x in ids[1:]:
            if uf.union(ids[0], x):
                merged += 1
                examples.append((by_id[ids[0]]["table_name"], by_id[x]["table_name"]))
                log.append(_entry("R1 표기 변형", by_id[ids[0]], by_id[x], "확정"))
    steps.append(("+ R1 표기 변형", uf.count(), merged, examples[:5]))

    # R2 대상 분할 ('-' 앞부분 + 분류명 구성)
    merged, examples = 0, []
    buckets3: dict[tuple[Any, ...], list[str]] = defaultdict(list)
    for t in tables:
        base = re.sub(r"\(\s*(계|남|여)\s*\)", "", edition_name(t["table_name"]))
        prefix, sep, suffix = base.rpartition("-")
        if not sep or not prefix.strip() or REGION_SUFFIX.match(suffix):
            continue
        buckets3[(t["survey_name"], loose_key(prefix), class_names(t))].append(t["tbl_id"])
    for ids in buckets3.values():
        if len({loose_key(by_id[i]["table_name"]) for i in ids}) < 2:
            continue
        for x in ids[1:]:
            if uf.union(ids[0], x):
                merged += 1
                log.append(_entry("R2 대상 분할", by_id[ids[0]], by_id[x], "확인 필요"))
        if len(examples) < 5:
            examples.append(("대상", len(ids), [by_id[i]["table_name"] for i in ids][:3]))
    steps.append(("+ R2 대상 분할", uf.count(), merged, examples))

    # R3 지역 분할 (같은 폴더·기간·분류명·항목, 이름 차이가 시도명뿐일 때만)
    merged, examples = 0, []
    review: Counter[str] = Counter()
    buckets2: dict[tuple[Any, ...], list[str]] = defaultdict(list)
    for t in tables:
        key = (t["survey_name"], folder(t), t["start_year"], t["end_year"], class_names(t),
               item_names(t))  # fmt: skip
        buckets2[key].append(t["tbl_id"])
    for ids in buckets2.values():
        if len(ids) < 2:
            continue
        names = [by_id[i]["table_name"] for i in ids]
        if len({loose_key(n) for n in names}) < 2:
            continue  # 이름이 같으면 R1에서 이미 처리
        verdict = "지역 분할" if only_region_differs(names) else "다른 데이터(묶지 않음)"
        review[verdict] += 1
        if verdict != "지역 분할":
            continue
        for x in ids[1:]:
            if uf.union(ids[0], x):
                merged += 1
                log.append(_entry("R3 지역 분할", by_id[ids[0]], by_id[x], verdict))
        if len(examples) < 5:
            examples.append((verdict, len(ids), names[:3]))
    steps.append(("+ R3 지역 분할", uf.count(), merged, examples))

    sizes = Counter(len(g) for g in uf.groups().values())
    md = _report(steps, review, sizes, len(tables))
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = f"{date.today():%Y%m%d}"
    (RESULTS / f"family_check_{stamp}.md").write_text(md, encoding="utf-8")
    with open(RESULTS / f"family_check_groups_{stamp}.csv", "w", newline="",
              encoding="utf-8-sig") as f:  # fmt: skip
        w = csv.DictWriter(f, fieldnames=list(log[0]))
        w.writeheader()
        w.writerows(log)
    print(md)


def _group(fam_of: dict[str, str]) -> dict[str, list[str]]:
    g: dict[str, list[str]] = defaultdict(list)
    for tbl, fam in fam_of.items():
        g[fam].append(tbl)
    return g


def _entry(rule: str, a: dict[str, Any], b: dict[str, Any], verdict: str) -> dict[str, Any]:
    return {
        "rule": rule,
        "verdict": verdict,
        "survey": a["survey_name"],
        "tbl_a": a["tbl_id"],
        "name_a": a["table_name"],
        "period_a": f"{a['start_year']}~{a['end_year']}",
        "tbl_b": b["tbl_id"],
        "name_b": b["table_name"],
        "period_b": f"{b['start_year']}~{b['end_year']}",
    }


def _report(steps: list[Any], review: Counter[str], sizes: Counter[int], n: int) -> str:
    lines = [
        f"# 패밀리 묶기 검증 ({date.today():%Y-%m-%d})",
        "",
        f"표 {n}개. 현재 `pipeline/build_families.py` 결과에 검색 규칙 1장의 미구현 규칙을 차례로 더했다.",
        "",
        "| 단계 | 패밀리 수 | 이번 단계에서 합쳐진 표 |",
        "| --- | --- | --- |",
    ]
    for label, count, merged, _ in steps:
        lines.append(f"| {label} | {count} | {merged or '—'} |")
    lines += ["", "## 단계별 예시", ""]
    for label, _, _, examples in steps[1:]:
        lines.append(f"### {label}")
        lines.append("")
        for e in examples:
            if len(e) == 2:
                lines.append(f"- {e[0]} = {e[1]}")
            else:
                lines.append(f"- [{e[0]}] {e[1]}개: " + " / ".join(e[2]))
        lines.append("")
    lines += [
        "## R3 판정",
        "",
        f"같은 폴더·기간·분류명·항목인 묶음 {sum(review.values())}개 중 이름 차이가 시도명뿐인"
        f" '지역 분할' {review['지역 분할']}개만 묶었다. 나머지"
        f" {review['다른 데이터(묶지 않음)']}개는 '혼인상태별'/'교육정도별'처럼 분류명은 같아도"
        " 내용이 다른 표라 묶지 않았다 (분류명이 '항목'·'성별' 같은 일반 이름이라 구조가 같게 보임).",
        "",
        "## 최종 패밀리 크기 분포",
        "",
        "| 표 수 | 패밀리 수 |",
        "| --- | --- |",
    ]
    for k in sorted(sizes):
        lines.append(f"| {k} | {sizes[k]} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
