# ruff: noqa: E501  (보고서 문장이 길어서)

import random
import re
from collections import Counter, defaultdict
from datetime import date
from itertools import combinations
from pathlib import Path
from typing import Any

from pipeline.build_families import build
from pipeline.load_catalog import read_catalog

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
PROJECTION = re.compile(r"추계|장래")
ITEM_OVERLAP = 0.2
GENERIC = {"계", "합계", "소계", "전체", "남자", "여자", "남", "여"}


def items(t: dict[str, Any]) -> set[str]:
    return {re.sub(r"\s+", "", i["name"]) for i in t["items"]} - GENERIC


def deepest(t: dict[str, Any]) -> str:
    return t["region_levels"][-1] if t["region_levels"] else "전국"


def main() -> None:
    tables = read_catalog()
    by = {t["tbl_id"]: t for t in tables}
    rows = build(tables)
    fam: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        fam[r["family_id"]].append(r)
    name = {f: rs[0]["family_name"] for f, rs in fam.items()}

    ids = Counter(r["tbl_id"] for r in rows)
    missing = [t["tbl_id"] for t in tables if t["tbl_id"] not in ids]
    dup = [i for i, n in ids.items() if n > 1]
    surveys_mixed = [
        f for f, rs in fam.items() if len({by[r["tbl_id"]]["survey_name"] for r in rs}) > 1
    ]

    level_mixed, proj_mixed, low_overlap = [], [], []
    for f, rs in fam.items():
        if len(rs) < 2:
            continue
        ts = [by[r["tbl_id"]] for r in rs]
        members: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
        for r in rs:
            members[(r["target"], r["sex"])].append(by[r["tbl_id"]])
        levels = {deepest(t) for t in ts}
        if len(levels) > 1:
            level_mixed.append(
                (
                    f,
                    sorted(
                        f"{t['tbl_id']}:{deepest(t)}({t['start_year']}~{t['end_year']})" for t in ts
                    ),
                )
            )
        if len({bool(PROJECTION.search(t["survey_name"] + t["table_name"])) for t in ts}) > 1:
            proj_mixed.append(f)
        for m_ts in members.values():
            for a, b in combinations(m_ts, 2):
                ia, ib = items(a), items(b)
                if ia and ib and len(ia & ib) / len(ia | ib) < ITEM_OVERLAP:
                    low_overlap.append((f, a, b, len(ia & ib) / len(ia | ib)))

    multi = [f for f, rs in fam.items() if len(rs) > 1]
    random.seed(20261102)
    sample = sorted(random.sample(multi, min(15, len(multi))), key=lambda f: name[f])
    biggest = sorted(multi, key=lambda f: -len(fam[f]))[:5]

    low_fams = sorted({x[0] for x in low_overlap})
    lines = [
        f"# 패밀리 맵핑 점검 ({date.today():%Y-%m-%d})",
        "",
        f"현재 `pipeline/build_families.py` 기준. 표 {len(tables)}개 → 패밀리 {len(fam)}개 (표 2개 이상 {len(multi)}개).",
        "",
        "## 결론",
        "",
        "| 점검 | 결과 |",
        "| --- | --- |",
        f"| 패밀리에 안 들어간 표 | {len(missing)}개 |",
        f"| 패밀리에 두 번 들어간 표 | {len(dup)}개 |",
        f"| 조사가 섞인 패밀리 | {len(surveys_mixed)}개 |",
        f"| 지역 수준이 섞인 패밀리 (확인 후보) | {len(level_mixed)}개 |",
        f"| 추계·실적이 섞인 패밀리 | {len(proj_mixed)}개 |",
        f"| 같은 멤버의 판끼리 항목이 거의 안 겹침 (확인 후보) | {len(low_fams)}개 패밀리, {len(low_overlap)}쌍 |",
        "",
        "## 2-1. 지역 수준이 섞인 패밀리",
        "",
        "규칙상 '-시도' / '-시군구'처럼 지역 수준만 다른 표는 다른 패밀리다. 여기 나온 것은 대부분 같은 표가"
        " 해마다 제공 단위가 달라진 경우(예: 옛 판은 시도까지, 새 판은 시군구까지)라 판으로 둬도 된다.",
        "",
    ]
    lines += [f"- **{name[f]}**: {', '.join(v)}" for f, v in level_mixed[:40]] or ["없음."]
    lines += ["", "## 2-2. 추계·실적이 섞인 패밀리", ""]
    lines += [
        f"- {name[f]}: " + ", ".join(by[r["tbl_id"]]["table_name"] for r in fam[f])
        for f in proj_mixed
    ] or ["없음."]
    lines += [
        "",
        "## 2-3. 같은 멤버의 판끼리 항목이 거의 안 겹침",
        "",
        f"항목 이름 겹침 비율이 {ITEM_OVERLAP} 미만. 판마다 항목 표기가 달라진 것인지(정상), 다른 데이터인지 봐야 한다.",
        "",
        "| 패밀리 | 표 A | 표 B | 겹침 |",
        "| --- | --- | --- | --- |",
    ]
    for f, a, b, j in sorted(low_overlap, key=lambda x: x[3])[:40]:
        ia = ", ".join(sorted(items(a))[:4])
        ib = ", ".join(sorted(items(b))[:4])
        lines.append(
            f"| {name[f]} | {a['tbl_id']} {a['start_year']}~{a['end_year']} ({ia}…) |"
            f" {b['tbl_id']} {b['start_year']}~{b['end_year']} ({ib}…) | {j:.2f} |"
        )
    lines += ["", "## 3. 표본 검수 (사람이 보고 맞는지 체크)", "", "### 표가 많은 패밀리", ""]

    def show(f: str) -> list[str]:
        out = [f"**{name[f]}** ({len(fam[f])}개, {by[fam[f][0]['tbl_id']]['survey_name']})", ""]
        for r in sorted(
            fam[f], key=lambda r: (r["target"] or "", r["sex"] or "", r["edition_order"])
        ):
            tag = " · ".join(x for x in (r["target"], r["sex"]) if x)
            out.append(
                f"- [ ] {r['tbl_id']} {by[r['tbl_id']]['table_name']} — {tag + ' · ' if tag else ''}{r['edition']}판"
            )
        return out + [""]

    for f in biggest:
        lines += show(f)
    lines += ["### 무작위 15개", ""]
    for f in sample:
        lines += show(f)
    md = "\n".join(lines) + "\n"
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"mapping_check_{date.today():%Y%m%d}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
