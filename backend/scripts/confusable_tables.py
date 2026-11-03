"""헷갈리는 표 분석: 서로 다른 패밀리인데 검색 문서가 아주 비슷한 표 쌍과, 그 쌍을 가르는 단어

목적
  남은 검색 오답의 대부분은 '초혼/재혼'처럼 한 단어만 다른 형제 표를 못 가르는 문제다
  (팀원 검색 실험 10/31). 어떤 표들이 서로 헷갈리고 무엇으로 갈리는지를 카탈로그에서 미리 찾아
  - search.py SUBSET_MODIFIERS 같은 구분 규칙의 근거
  - 리랭커 학습용 하드 네거티브 목록
  - 골든셋에 넣을 형제 표 문항 후보
  로 쓴다.

방법
  1. 표마다 검색 문서(pipeline/search_docs.py, 기본 full)를 만든다
  2. 유사도: --vectors를 주면 그 임베딩(build_embeddings 캐시 .npy/.ids), 없으면 글자 n-gram TF-IDF
  3. 패밀리가 다르고 유사도가 기준 이상인 쌍을 뽑는다 (같은 패밀리는 판·멤버라 F-18이 처리)
  4. 두 표명을 단어로 나눠 한쪽에만 있는 단어(구분 단어)를 뽑고 유형으로 나눈다

입력: data/catalog/population_table_catalog_with_meta.jsonl (DB 불필요)
출력: eval/results/confusable_pairs_YYYYMMDD.csv  (쌍 목록)
      eval/results/confusable_report_YYYYMMDD.md  (요약)

실행
  python -m scripts.confusable_tables                       # TF-IDF
  python -m scripts.confusable_tables --vectors data/embeddings/bge-m3__full.npy
  python -m scripts.confusable_tables --threshold 0.9 --preset full
"""

import argparse
import csv
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from pipeline.build_families import build as build_families
from pipeline.build_families import edition_name
from pipeline.load_catalog import read_catalog
from pipeline.search_docs import PRESETS
from pipeline.search_docs import build as build_doc

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"

# 표명을 단어로 나누는 구분자 ('시도/성/연령별', '성, 연령 및 세대구성별', '…-시군구')
SPLIT = re.compile(r"[/,·ㆍ\s\-()]+|및")
# 구분 단어 유형 (위에서부터 먼저 맞는 것)
KINDS: list[tuple[str, re.Pattern[str]]] = [
    ("지역 단위", re.compile(r"^(시도|시군구|읍면동|행정구역|전국|권역|도시|동|읍|면|시|군|구)$")),
    ("기준·정의", re.compile(r"기준|발생|신고|등록|주민등록|현거주지|상주|내국인|외국인|전수|표본")),
    ("모집단 수식어", re.compile(r"초혼|재혼|첫|쌍|다태|북한|귀화|이혼|사별|미혼|1인|고령|아동|청년|여성|남성|장애|다문화")),  # noqa: E501
    ("지표 형태", re.compile(r"율|률|비|수$|건수|지수|평균|중위|구성비|비중|비율|인구$|가구$")),
]
OTHER_KIND = "분류 축·기타"


def name_tokens(name: str) -> set[str]:
    """판 표기를 지운 표명 → 단어 집합 (끝의 '별' 제거, '연령별' → '연령')"""
    words = (w.removesuffix("별") for w in SPLIT.split(edition_name(name)))
    return {w for w in words if w}


def kind_of(word: str) -> str:
    for kind, pattern in KINDS:
        if pattern.search(word):
            return kind
    return OTHER_KIND


def tfidf_similarity(docs: list[str]) -> np.ndarray:
    from sklearn.feature_extraction.text import TfidfVectorizer

    m = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 3), sublinear_tf=True).fit_transform(
        docs
    )
    return (m @ m.T).toarray()


def vector_similarity(path: Path, ids: list[str]) -> np.ndarray:
    cached = path.with_suffix(".ids").read_text().split()
    index = {t: i for i, t in enumerate(cached)}
    missing = [t for t in ids if t not in index]
    if missing:
        raise SystemExit(f"벡터 캐시에 없는 표 {len(missing)}개 (예: {missing[:3]})")
    v = np.load(path)[[index[t] for t in ids]]
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    return v @ v.T


def pairs(
    tables: list[dict[str, Any]], fam: dict[str, str], sim: np.ndarray, threshold: float
) -> list[dict[str, Any]]:
    out = []
    n = len(tables)
    rows, cols = np.where(np.triu(sim, k=1) >= threshold)
    for i, j in zip(rows.tolist(), cols.tolist(), strict=True):
        a, b = tables[i], tables[j]
        if fam[a["tbl_id"]] == fam[b["tbl_id"]]:
            continue
        ta, tb = name_tokens(a["table_name"]), name_tokens(b["table_name"])
        only_a, only_b = sorted(ta - tb), sorted(tb - ta)
        out.append(
            {
                "similarity": round(float(sim[i, j]), 4),
                "same_survey": a["survey_name"] == b["survey_name"],
                "same_folder": _folder(a) == _folder(b),
                "survey_a": a["survey_name"],
                "tbl_a": a["tbl_id"],
                "name_a": a["table_name"],
                "survey_b": b["survey_name"],
                "tbl_b": b["tbl_id"],
                "name_b": b["table_name"],
                "only_a": " ".join(only_a),
                "only_b": " ".join(only_b),
                "diff_size": len(only_a) + len(only_b),
            }
        )
    assert n == sim.shape[0]
    return sorted(out, key=lambda p: -p["similarity"])


def _folder(t: dict[str, Any]) -> str:
    paths = t.get("path_texts") or [""]
    return paths[0].rsplit(" > ", 1)[0]


def groups(found: list[dict[str, Any]]) -> list[set[str]]:
    """헷갈리는 쌍을 이어 만든 표 묶음 (union-find)"""
    parent: dict[str, str] = {}

    def root(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in found:
        parent[root(p["tbl_a"])] = root(p["tbl_b"])
    members: dict[str, set[str]] = defaultdict(set)
    for x in parent:
        members[root(x)].add(x)
    return sorted(members.values(), key=len, reverse=True)


def report(
    tables: list[dict[str, Any]],
    fam: dict[str, str],
    sim: np.ndarray,
    found: list[dict[str, Any]],
    method: str,
    threshold: float,
) -> str:
    names = {t["tbl_id"]: t["table_name"] for t in tables}
    n = len(tables)
    # 표마다 다른 패밀리 중 가장 가까운 표와의 유사도
    same = np.array([[fam[a["tbl_id"]] == fam[b["tbl_id"]] for b in tables] for a in tables])
    other = np.where(same, -1.0, sim)
    nearest = other.max(axis=1)
    near_lines = [
        f"| {th} | {(nearest >= th).sum()} | {(nearest >= th).mean():.0%} |"
        for th in (0.95, 0.9, 0.85, 0.8)
    ]

    one_word = [p for p in found if p["diff_size"] == 1]
    word_count: Counter[str] = Counter()
    kind_count: Counter[str] = Counter()
    for p in found:
        words = (p["only_a"] + " " + p["only_b"]).split()
        for w in words:
            word_count[w] += 1
        kinds = {kind_of(w) for w in words} or {"표명 같음(분류·항목으로만 갈림)"}
        for k in kinds:
            kind_count[k] += 1
    by_kind: dict[str, Counter[str]] = defaultdict(Counter)
    for w, c in word_count.items():
        by_kind[kind_of(w)][w] += c

    gs = groups(found)
    lines = [
        f"# 헷갈리는 표 분석 ({date.today():%Y-%m-%d})",
        "",
        f"- 표 {n}개, 패밀리 {len(set(fam.values()))}개 (현재 `pipeline/build_families.py` 기준)",
        f"- 유사도: {method}, 기준 {threshold} 이상이고 **패밀리가 다른** 쌍만",
        f"- 찾은 쌍 {len(found)}개 · 같은 조사 {sum(p['same_survey'] for p in found)}"
        f" · 같은 폴더 {sum(p['same_folder'] for p in found)}"
        f" · 표명이 한 단어만 다른 쌍 {len(one_word)}",
        "",
        "## 1. 다른 패밀리 중 가장 가까운 표와의 유사도",
        "",
        "| 기준 | 표 수 | 비율 |",
        "| --- | --- | --- |",
        *near_lines,
        "",
        "## 2. 무엇으로 갈리나 (쌍 단위, 한 쌍이 여러 유형에 들 수 있음)",
        "",
        "| 유형 | 쌍 수 | 자주 나오는 구분 단어 |",
        "| --- | --- | --- |",
    ]
    for k, c in kind_count.most_common():
        top = ", ".join(f"{w}({n})" for w, n in by_kind.get(k, Counter()).most_common(10))
        lines.append(f"| {k} | {c} | {top or '—'} |")
    lines += [
        "",
        "## 3. 헷갈리는 표 묶음 (쌍을 이은 것, 큰 순서)",
        "",
        f"묶음 {len(gs)}개 · 2개짜리 {sum(len(g) == 2 for g in gs)}"
        f" · 5개 이상 {sum(len(g) >= 5 for g in gs)}",
        "",
    ]
    for g in gs[:15]:
        sample = sorted(names[t] for t in g)[:6]
        lines.append(f"- **{len(g)}개**: " + " / ".join(sample) + (" …" if len(g) > 6 else ""))
    lines += [
        "",
        "## 4. 표명이 한 단어만 다른 쌍 (상위 30)",
        "",
        "| 유사도 | 표 A | 표 B | 구분 단어 |",
        "| --- | --- | --- | --- |",
    ]
    for p in one_word[:30]:
        lines.append(
            f"| {p['similarity']:.3f} | {p['name_a']} | {p['name_b']} |"
            f" {p['only_a'] or '—'} / {p['only_b'] or '—'} |"
        )
    lines += [
        "",
        "## 5. 표기만 다른데 다른 패밀리인 쌍 (구분자·'및'·'별'만 다름)",
        "",
        "검색 규칙 1.1 기준 ②(표기 변형은 같은 판)대로라면 같은 패밀리여야 하는데, 현재 name_key는 기호·띄어쓰기만 지우고 '및'·'별'은 남겨 갈라진 경우다. 예: '성, 연령 및 직업별' / '성별/연령별/직업별'.",
        "",
    ]
    same_name = [p for p in found if p["diff_size"] == 0]
    lines += [f"{len(same_name)}쌍 (같은 조사 {sum(p['same_survey'] for p in same_name)})", ""]
    for p in same_name[:10]:
        lines.append(f"- {p['name_a']} ({p['tbl_a']}) = {p['name_b']} ({p['tbl_b']})")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--preset", choices=PRESETS, default="full")
    parser.add_argument("--threshold", type=float, default=0.85)
    parser.add_argument("--vectors", type=Path, help="임베딩 캐시 .npy (같은 이름의 .ids 필요)")
    args = parser.parse_args()

    tables = read_catalog()
    fam = {r["tbl_id"]: r["family_id"] for r in build_families(tables)}
    ids = [t["tbl_id"] for t in tables]
    if args.vectors:
        sim, method = vector_similarity(args.vectors, ids), f"임베딩 {args.vectors.name}"
    else:
        docs = [build_doc(t, args.preset) for t in tables]
        sim, method = tfidf_similarity(docs), f"글자 2~3그램 TF-IDF ({args.preset} 문서)"
    found = pairs(tables, fam, sim, args.threshold)

    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = f"{date.today():%Y%m%d}"
    with open(RESULTS / f"confusable_pairs_{stamp}.csv", "w", newline="", encoding="utf-8-sig") as f:
        if found:
            w = csv.DictWriter(f, fieldnames=list(found[0]))
            w.writeheader()
            w.writerows(found)
    md = report(tables, fam, sim, found, method, args.threshold)
    (RESULTS / f"confusable_report_{stamp}.md").write_text(md, encoding="utf-8")
    print(md)


if __name__ == "__main__":
    main()
