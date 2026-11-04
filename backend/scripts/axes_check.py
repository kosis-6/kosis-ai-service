# ruff: noqa: E501  (보고서 문장이 길어서)
"""패밀리 선택지(family_axes) 점검: 패밀리마다 고를 수 있는 칸이 제대로 만들어졌나

검색 결과를 패밀리로 접은 뒤 챗봇은 패밀리의 축(대상 target / 성별 sex / 판 edition)과 선택지로
표 하나를 고르고, 기본값이 없으면 되묻는다(F-18, F-14). 축은 app/services/search.family_axes와
같은 방식으로 만든다 (선택지가 2개 이상인 축만).

  1 축 구성     패밀리마다 어떤 축이 있나
  2 기본값      대상 축에 '전체', 성별 축에 '계'가 있나 (없으면 되묻기 대상)
  3 이상한 이름  대상 선택지가 지역 수준 단어·빈 괄호·너무 긴 이름·뜻이 안 드러나는 이름('기타')인가,
                한 패밀리에 표기만 다른 같은 대상('결혼이민자'/'결혼 이민자')이 있나,
                지역 이름과 다른 대상이 섞였나
  4 성별        선택지가 계/남/여 밖이거나, 남·여만 있고 계가 없나
  5 빈칸        대상 × 성별 조합 중 빠진 칸 (예: 기타는 계·남만 있음)
  6 판          같은 패밀리의 멤버끼리 판(연도) 구성이 다른가 (멤버를 바꾸면 고를 수 있는 연도가 달라짐)

입력: data/catalog/population_table_catalog_with_meta.jsonl (DB 불필요)
출력: eval/results/axes_check_YYYYMMDD.md
실행: python -m scripts.axes_check
"""

import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from pipeline.build_families import SIDO, build
from pipeline.load_catalog import read_catalog

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
REGION_LEVEL = re.compile(r"^(시도|시군구|읍면동|전국)$")
VAGUE = re.compile(r"^(기타|기타 외국인|그 외|나머지)$")
LONG = 20


def norm(v: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", v or "")


def axes_of(rows: list[dict[str, Any]], by: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """app/services/search.family_axes와 같은 규칙"""

    def options(key: str) -> list[str]:
        values = {r[key] for r in rows if r[key] is not None}
        return sorted(values, key=lambda v: (v != "전체" and v != "계", v))

    axes = {"target": options("target"), "sex": options("sex")}
    editions = sorted(
        {(-by[r["tbl_id"]]["end_year"], by[r["tbl_id"]]["start_year"], r["edition"]) for r in rows}
    )
    axes["edition"] = list(dict.fromkeys(e for _, _, e in editions))
    return {k: v for k, v in axes.items() if len(v) > 1}


def main() -> None:
    tables = read_catalog()
    by = {t["tbl_id"]: t for t in tables}
    rows = build(tables)
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        families[r["family_id"]].append(r)
    name = {f: rs[0]["family_name"] for f, rs in families.items()}
    axes = {f: axes_of(rs, by) for f, rs in families.items()}

    # 1. 축 구성
    combo = Counter(" + ".join(a) if a else "(없음: 표 1개 또는 판 1개)" for a in axes.values())

    # 2. 기본값
    no_target_default = [f for f, a in axes.items() if "target" in a and "전체" not in a["target"]]
    no_sex_default = [f for f, a in axes.items() if "sex" in a and "계" not in a["sex"]]

    # 3. 이상한 대상 이름
    odd: list[tuple[str, str, str]] = []
    for f, a in axes.items():
        targets = [t for t in a.get("target", []) if t != "전체"]
        for t in targets:
            if REGION_LEVEL.match(t):
                odd.append((f, t, "지역 수준 단어"))
            elif re.search(r"\(\s*\)|^\W|\W$", t) and not t.endswith(")"):
                odd.append((f, t, "기호로 시작·끝남"))
            elif len(t) > LONG:
                odd.append((f, t, f"{LONG}자 넘음 (질문에 그대로 나오기 어려움)"))
            elif VAGUE.match(t):
                odd.append((f, t, "뜻이 안 드러남 (주석 필요)"))
        seen: dict[str, str] = {}
        for t in targets:
            k = norm(t)
            if k in seen and seen[k] != t:
                odd.append((f, f"{seen[k]} / {t}", "표기만 다른 같은 대상"))
            seen.setdefault(k, t)
        regional = [t for t in targets if SIDO.fullmatch(t.split("·")[0])]
        if regional and len(regional) != len(targets):
            odd.append((f, " / ".join(targets[:6]), "지역 이름과 다른 대상이 섞임"))

    # 4. 성별
    bad_sex = [
        (f, a["sex"])
        for f, a in axes.items()
        if "sex" in a and not set(a["sex"]) <= {"계", "남", "여"}
    ]

    # 5. 대상 × 성별 빈칸
    holes: list[tuple[str, str, list[str]]] = []
    for f, a in axes.items():
        if "target" in a and "sex" in a:
            have = {(r["target"], r["sex"]) for r in families[f]}
            for t in a["target"]:
                missing = [s for s in a["sex"] if (t, s) not in have]
                if missing:
                    holes.append((f, t, missing))

    # 6. 멤버끼리 판 구성이 다른 패밀리
    diff_editions: list[tuple[str, dict[tuple[Any, Any], list[str]]]] = []
    for f, rs in families.items():
        members: dict[tuple[Any, Any], set[str]] = defaultdict(set)
        for r in rs:
            members[(r["target"], r["sex"])].add(r["edition"])
        if len(members) > 1 and len({frozenset(v) for v in members.values()}) > 1:
            diff_editions.append((f, {k: sorted(v) for k, v in members.items()}))

    multi_member = [f for f, a in axes.items() if "target" in a or "sex" in a]

    lines = [
        f"# 패밀리 선택지 점검 ({date.today():%Y-%m-%d})",
        "",
        f"현재 `pipeline/build_families.py` 기준. 패밀리 {len(families)}개, 그중 멤버(대상·성별·지역)로 나뉜 패밀리 {len(multi_member)}개.",
        "축은 `search.family_axes`와 같은 규칙(선택지가 2개 이상인 축만)으로 만들었다.",
        "",
        "## 결론",
        "",
        "| 점검 | 결과 |",
        "| --- | --- |",
        f"| 대상 축에 '전체'가 없는 패밀리 (되묻기 대상) | {len(no_target_default)}개 |",
        f"| 성별 축에 '계'가 없는 패밀리 | {len(no_sex_default)}개 |",
        f"| 이상한 대상 이름 | {len(odd)}건 |",
        f"| 성별 선택지가 계/남/여 밖 | {len(bad_sex)}건 |",
        f"| 대상 × 성별 빈칸 | {len(holes)}건 |",
        f"| 멤버끼리 판(연도) 구성이 다른 패밀리 | {len(diff_editions)}개 |",
        "",
        "## 1. 축 구성",
        "",
        "| 축 | 패밀리 수 |",
        "| --- | --- |",
        *[f"| {k} | {v} |" for k, v in combo.most_common()],
        "",
        "## 2. 기본값이 없는 패밀리 (되묻기 대상)",
        "",
        "| 패밀리 | 대상 선택지 |",
        "| --- | --- |",
        *[f"| {name[f]} | {' / '.join(axes[f]['target'])} |" for f in no_target_default],
    ]
    if no_sex_default:
        lines += ["", "성별 '계'가 없는 패밀리:", ""]
        lines += [f"- {name[f]}: {' / '.join(axes[f]['sex'])}" for f in no_sex_default]
    lines += ["", "## 3. 이상한 대상 이름", ""]
    if odd:
        lines += ["| 패밀리 | 대상 | 이유 |", "| --- | --- | --- |"]
        lines += [f"| {name[f]} | {t} | {why} |" for f, t, why in odd]
    else:
        lines.append("없음.")
    lines += ["", "## 4. 성별 선택지", ""]
    lines += [f"- {name[f]}: {s}" for f, s in bad_sex] or ["모두 계/남/여 안에 있음."]
    lines += ["", "## 5. 대상 × 성별 빈칸", ""]
    if holes:
        lines.append("이 대상을 고르면 빠진 성별은 고를 수 없다. 챗봇은 계로 대신한다.")
        lines += ["", "| 패밀리 | 대상 | 없는 성별 |", "| --- | --- | --- |"]
        lines += [f"| {name[f]} | {t} | {', '.join(m)} |" for f, t, m in holes]
    else:
        lines.append("없음.")
    lines += ["", "## 6. 멤버끼리 판(연도) 구성이 다른 패밀리", ""]
    if diff_editions:
        lines.append(
            "멤버를 바꾸면 고를 수 있는 연도판이 달라진다. 예: 어떤 대상은 2015년까지만 있음."
        )
        lines += [""]
        for f, members in diff_editions:
            parts = "; ".join(
                f"{'/'.join(x for x in k if x) or '(기본)'}: {', '.join(v)}"
                for k, v in members.items()
            )
            lines.append(f"- **{name[f]}** — {parts}")
    else:
        lines.append("없음.")
    md = "\n".join(lines) + "\n"
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"axes_check_{date.today():%Y%m%d}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
