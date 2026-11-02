# ruff: noqa: E501  (보고서 문장이 길어서)
"""판(연도) 묶기 검증: 연도만 다른 표가 같은 패밀리·같은 멤버의 다른 판으로 들어가 있는가

검색 규칙 1.1: 같은 데이터에서 수록 기간만 다른 표는 같은 멤버의 판이다. 이를 네 가지로 확인한다.

  A 같은 조사 + 이름 속 연도 표기만 다른 표 ('…(2007~2015)' / '…')  → 같은 멤버여야 한다
  B 같은 멤버 안에 수록 기간이 똑같은 판이 둘 이상                     → 판으로 구분되지 않는다
  C 같은 멤버 안에 수록 기간이 겹치는 판                                → 연도로 판을 고를 때 둘 다 맞는다
  D 이름의 어느 자리든 연도만 다르고(예: '2010년 행정구역 기준') 아직 다른 멤버인 표 → 사람이 확인

그리고 패밀리 전체가 어떻게 처리돼 있는지(멤버·판 구성, 기본 판 규칙)를 요약한다.

입력: data/catalog/population_table_catalog_with_meta.jsonl (DB 불필요)
출력: eval/results/edition_check_YYYYMMDD.md
실행: python -m scripts.edition_check
"""

import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.build_families import build, edition_name, name_key
from pipeline.load_catalog import read_catalog

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
# 이름 어디에 있든 연도 표기 ('2010년', '(2007~2015)', '2015년 이후', '(~1992)')
ANY_YEAR = re.compile(
    r"\(?\s*~?\s*(?:19|20)\d{2}\s*년?\s*(?:이후|이전|기준)?\s*(?:~\s*(?:(?:19|20)\d{2}\s*년?)?)?\s*\)?"
)


def period(t: dict[str, Any]) -> tuple[int, int]:
    return t["start_year"], t["end_year"]


def main() -> None:
    tables = read_catalog()
    by = {t["tbl_id"]: t for t in tables}
    rows = build(tables)
    fam = {r["tbl_id"]: r for r in rows}

    def member(tbl: str) -> tuple[str, str | None, str | None]:
        r = fam[tbl]
        return r["family_id"], r["target"], r["sex"]

    # A. 이름 속 연도 표기만 다른 표
    groups_a: dict[tuple[str, str], list[str]] = defaultdict(list)
    for t in tables:
        groups_a[(t["survey_name"], edition_name(t["table_name"]))].append(t["tbl_id"])
    multi_a = {k: v for k, v in groups_a.items() if len(v) > 1}
    split_a = {k: v for k, v in multi_a.items() if len({member(i) for i in v}) > 1}
    with_year = [
        k for k, v in multi_a.items() if any(by[i]["table_name"] != k[1] for i in v)
    ]  # 이름에 연도 표기가 실제로 있던 묶음

    # B·C. 멤버 안의 판
    members: dict[tuple[str, str | None, str | None], list[str]] = defaultdict(list)
    for r in rows:
        members[(r["family_id"], r["target"], r["sex"])].append(r["tbl_id"])
    same_period, overlap = [], []
    for key, ids in members.items():
        ps = Counter(period(by[i]) for i in ids)
        for p, n in ps.items():
            if n > 1:
                same_period.append((key, p, [i for i in ids if period(by[i]) == p]))
        spans = sorted({period(by[i]) for i in ids})
        for (s1, e1), (s2, e2) in zip(spans, spans[1:], strict=False):
            if s2 <= e1:
                overlap.append((key, (s1, e1), (s2, e2)))

    # D. 이름 어디든 연도만 다른데 다른 멤버
    groups_d: dict[tuple[str, str], list[str]] = defaultdict(list)
    for t in tables:
        groups_d[(t["survey_name"], name_key(ANY_YEAR.sub("", t["table_name"])))].append(
            t["tbl_id"]
        )
    # 이름이 연도까지 같은 표(기호만 다른 다른 표, B에서 나눈 것)는 빼고 연도가 실제로 다른 것만
    split_d = [
        v
        for v in groups_d.values()
        if len({member(i) for i in v}) > 1
        and len({name_key(edition_name(by[i]["table_name"])) for i in v}) > 1
    ]

    # 패밀리 요약
    fam_size = Counter(r["family_id"] for r in rows)
    member_count = Counter(k[0] for k in members)
    editions_per_member = Counter(len(v) for v in members.values())
    order_ok = all(
        min(ids, key=lambda i: fam[i]["edition_order"])
        == min(ids, key=lambda i: (-by[i]["end_year"], by[i]["start_year"], i))
        for ids in members.values()
    )

    lines = [
        f"# 판(연도) 묶기 검증 ({date.today():%Y-%m-%d})",
        "",
        "현재 `pipeline/build_families.py` 기준. 카탈로그 2,364개 표.",
        "",
        "## 결론",
        "",
        "| 확인 | 결과 |",
        "| --- | --- |",
        f"| A. 이름 속 연도 표기만 다른 표가 같은 멤버인가 | 묶음 {len(multi_a)}개 중"
        f" 갈라진 묶음 **{len(split_a)}개** |",
        f"| B. 한 멤버에 수록 기간이 똑같은 판 | **{len(same_period)}건** |",
        f"| C. 한 멤버에 수록 기간이 겹치는 판 | **{len(overlap)}건** |",
        f"| D. 이름 어디든 연도만 다른데 다른 멤버 | **{len(split_d)}묶음** (사람이 확인) |",
        f"| 기본 판(edition_order 1) = 최신 연도 → 기간 긴 판 | {'맞음' if order_ok else '틀림'} |",
        "",
        "## A. 이름 속 연도 표기만 다른 표",
        "",
        f"같은 조사 + 연도 표기('(2007~2015)', '(2006)', '(신)/(구)')를 지운 이름이 같은 묶음 {len(multi_a)}개"
        f" (그중 이름에 연도 표기가 실제로 있던 묶음 {len(with_year)}개, 나머지는 이름이 완전히 같은 연도판).",
        "",
    ]
    if split_a:
        lines += ["갈라진 묶음:", ""]
        for (survey, name), ids in list(split_a.items())[:20]:
            parts = ", ".join(
                f"{i}({fam[i]['family_id']}/{fam[i]['target']}/{fam[i]['sex']})" for i in ids
            )
            lines.append(f"- {survey} · {name}: {parts}")
    else:
        lines.append("→ 전부 같은 패밀리·같은 멤버의 다른 판으로 들어가 있다.")
    lines += ["", "예시 (판이 많은 멤버):", "", "| 멤버 | 판 (기본 판 먼저) |", "| --- | --- |"]
    for key, ids in sorted(members.items(), key=lambda kv: -len(kv[1]))[:8]:
        ids = sorted(ids, key=lambda i: fam[i]["edition_order"])
        name = fam[ids[0]]["family_name"]
        label = name + (f" · {key[1]}" if key[1] else "") + (f" · {key[2]}" if key[2] else "")
        eds = " / ".join(f"{fam[i]['edition']}({i})" for i in ids)
        lines.append(f"| {label} | {eds} |")

    lines += ["", "## B. 한 멤버에 수록 기간이 똑같은 판", ""]
    if same_period:
        lines.append(
            "같은 기간의 판이 둘이면 연도로 고를 수 없다. 판이 아니라 **이름 표기가 비슷한 다른 표**가"
            " 잘못 묶였을 수 있으니 분류 구성을 비교한다."
        )
        lines += ["", "| 기간 | 표 | 분류 |", "| --- | --- | --- |"]
        for _, p, ids in same_period[:30]:
            for i in ids:
                cls = ", ".join(c["name"] for c in by[i]["classifications"])
                lines.append(f"| {p[0]}~{p[1]} | {by[i]['table_name']} ({i}) | {cls} |")
    else:
        lines.append("없음.")

    lines += ["", "## C. 한 멤버에 수록 기간이 겹치는 판", ""]
    if overlap:
        lines.append(
            "연도를 물으면 두 판이 모두 맞는다. 규칙 2.4대로 기간이 긴 판 → 최신 시점이 늦은 판을 고른다."
        )
        lines += ["", "| 멤버 | 판 1 | 판 2 |", "| --- | --- | --- |"]
        for key, a, b in overlap[:30]:
            ids = members[key]
            lines.append(f"| {fam[ids[0]]['family_name']} | {a[0]}~{a[1]} | {b[0]}~{b[1]} |")
    else:
        lines.append("없음.")

    lines += ["", "## D. 이름 어디든 연도만 다른데 다른 멤버 (사람이 확인)", ""]
    if split_d:
        lines.append(
            "'2010년 행정구역 기준'처럼 연도가 판이 아니라 **기준**을 뜻하면 다른 표가 맞다."
        )
        lines += [""]
        for ids in split_d[:30]:
            parts = " / ".join(
                f"{by[i]['table_name']} ({by[i]['start_year']}~{by[i]['end_year']})" for i in ids
            )
            lines.append(f"- {parts}")
    else:
        lines.append("없음.")

    lines += [
        "",
        "## 패밀리 처리 현황",
        "",
        "| 항목 | 값 |",
        "| --- | --- |",
        f"| 패밀리 | {len(fam_size)}개 (표 1개 {sum(n == 1 for n in fam_size.values())},"
        f" 2개 이상 {sum(n > 1 for n in fam_size.values())}) |",
        f"| 멤버 | {len(members)}개 (멤버가 2개 이상인 패밀리 {sum(n > 1 for n in member_count.values())}) |",
        f"| 판이 2개 이상인 멤버 | {sum(n for k, n in editions_per_member.items() if k > 1)}개 |",
        "| 멤버당 판 수 | "
        + ", ".join(f"{k}개 {v}" for k, v in sorted(editions_per_member.items()))
        + " |",
        f"| 이름이 바뀐 후속판 연결 | {sum(r['renamed_from'] is not None for r in rows)}개 표 |",
        "",
        "처리 순서 (pipeline/build_families.py)",
        "",
        "1. **판**: 같은 조사 + 이름에서 연도 표기·(신)/(구)를 지우고 띄어쓰기·기호·'및'·'의'·'별'을 지운 이름이 같으면"
        " 같은 멤버의 판. 표 ID가 옛 ID + '_A'이고 분류 구성이 같으면 이름이 바뀐 후속판으로 붙인다.",
        "2. **멤버**: 이름 끝 (계)/(남)/(여) → 성별, '-' 뒤 → 대상, 시도 이름만 다른 같은 구조 → 지역 (#65).",
        "3. **패밀리**: 멤버 이름에서 성별·대상·지역을 뗀 이름이 같으면 한 패밀리.",
        "4. **기본 판**: 멤버 안에서 최신 연도를 담은 판 → 그중 기간이 긴 판이 edition_order 1.",
    ]
    md = "\n".join(lines) + "\n"
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"edition_check_{date.today():%Y%m%d}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
