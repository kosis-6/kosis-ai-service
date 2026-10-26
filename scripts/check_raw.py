"""원본 CSV ↔ 서비스(DuckDB) 값 대조: 같은 분자·분모·기간으로 계산하는지 전수 확인 (멘토링 질문 2)

전처리·서비스 코드를 쓰지 않고 수집 원본 CSV(data/processed/<표ID>.csv)를 pandas로 직접 읽어
지표를 다시 계산한 뒤, 서비스가 실제로 쓰는 함수의 값과 비교한다.

  1. 계산 지표 (DT_1B04006 주민등록인구): 총인구, 고령인구 비율, 유소년인구 비율, 노령화지수
     분자 = 65세 이상(100세 이상 포함) 또는 0~14세 인구 합, 분모 = 연령 '계' 행
     모든 연도 × 모든 지역 × 성별(계·남·여). 연령별 합 = '계'인지도 함께 본다
  2. 조회 지표 (지표 사전 method = "lookup"): 지표 사전의 표·항목·필터 그대로 원본에서 더하고,
     분모가 있으면 (값 ÷ 분모) × 100. 전국·시도 전체 연도. 구성비 지표는 대상이 아니다
  3. 기간: 원본의 시점 범위와 서비스가 안내하는 수록 기간이 같은지

실행: python -m scripts.check_raw
결과: data/eval/raw_check_<시각>.md
"""

import re
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd

from app.core.duckdb import rows as db_rows
from app.services import indicators, lookup

RAW_DIR = Path("data/processed")
OUT_DIR = Path("data/eval")
POP_SEX = {"T2": "계", "T3": "남", "T4": "여"}
TOLERANCE = 1e-6  # 반올림 전 값끼리 비교


@lru_cache
def raw(tbl_id: str) -> pd.DataFrame:
    """원본 CSV. 통계부호(value_flag)가 있는 칸은 결측"""
    df = pd.read_csv(RAW_DIR / f"{tbl_id}.csv", dtype=str)
    df["v"] = pd.to_numeric(df["value"], errors="coerce")
    df.loc[df["value_flag"].notna() & (df["value_flag"] != ""), "v"] = float("nan")
    df["year"] = df["period"].str[:4].astype(int)
    return df


def _age(name: str) -> int | None:
    if name == "계":
        return None
    return int(re.match(r"\d+", name).group())


# ---- 1. 계산 지표 ----------------------------------------------------------------


def check_population() -> list[str]:
    df = raw(indicators.POP_TABLE).copy()
    df["sex"] = df["item_id"].map(POP_SEX)
    df["age"] = df["c2_nm"].map(_age)
    keys = ["year", "c1_id", "sex"]
    total = df[df["age"].isna()].groupby(keys)["v"].sum(min_count=1)
    ages = df[df["age"].notna()]
    young = ages[ages["age"] <= 14].groupby(keys)["v"].sum(min_count=1)
    old = ages[ages["age"] >= 65].groupby(keys)["v"].sum(min_count=1)
    age_sum = ages.groupby(keys)["v"].sum(min_count=1)
    want = pd.DataFrame({"total": total, "young": young, "old": old, "age_sum": age_sum})
    want["고령인구 비율"] = want["old"] / want["total"] * 100
    want["유소년인구 비율"] = want["young"] / want["total"] * 100
    want["노령화지수"] = want["old"] / want["young"] * 100
    want["총인구"] = want["total"]

    lines = []
    sum_off = want[(want["age_sum"] - want["total"]).abs() > 0]
    lines.append(f"- 연령별 합 = 연령 '계' (분모 일치): 어긋난 행 {len(sum_off)}개 / {len(want)}개")

    lo, hi = int(df["year"].min()), int(df["year"].max())
    codes = sorted(df["c1_id"].unique())
    for name in ("총인구", "고령인구 비율", "유소년인구 비율", "노령화지수"):
        ind = indicators.INDICATORS[name]
        bad, n = [], 0
        for sex in ("계", "남", "여"):
            comps = indicators._components(codes, lo, hi, sex)  # 서비스가 쓰는 조회
            for (year, code), comp in comps.items():
                got = ind["calc"](comp)
                exp = want[name].get((year, code, sex))
                n += 1
                if got is None or exp is None or pd.isna(exp):
                    if not (got is None and (exp is None or pd.isna(exp))):
                        bad.append((year, code, sex, got, exp))
                elif abs(got - exp) > TOLERANCE * max(1, abs(exp)):
                    bad.append((year, code, sex, got, exp))
        example = f" (예: {bad[:3]})" if bad else ""
        lines.append(f"- {name}: {n}개 비교, 불일치 {len(bad)}개{example}")
    return lines


# ---- 2. 조회 지표 ----------------------------------------------------------------


def _raw_sum(df: pd.DataFrame, items, filters: dict, by_region: bool = True) -> pd.Series:
    items = items if isinstance(items, list) else [items]
    sel = df[df["item_id"].isin(items)]
    for col, val in filters.items():
        vals = val if isinstance(val, list) else [val]
        sel = sel[sel[col].isin(vals)]
    if by_region:
        return sel.groupby(["year", "c1_id"])["v"].sum(min_count=1)
    return sel.groupby("year")["v"].sum(min_count=1)  # 지역 구분이 없는 전국 표


def _expected(src: dict) -> pd.Series:
    df = raw(src["table"])
    filters = dict(src.get("filters", {}))
    if src.get("age_dim"):
        filters[f"{src['age_dim']}_id"] = src["age_total"]
    by_region = not src.get("national_only")
    num = _raw_sum(df, src["item_id"], filters, by_region)
    if not src.get("den_item_id"):
        return num
    den_filters = dict(src.get("den_filters") or src.get("filters", {}))
    if src.get("age_dim"):
        den_filters[f"{src['age_dim']}_id"] = src["age_total"]
    den = _raw_sum(df, src["den_item_id"], den_filters, by_region)
    return num / den * 100


def check_lookup() -> list[str]:
    lines = []
    for name, spec in indicators.specs().items():
        if spec["method"] != "lookup" or lookup.kind_of(spec) == "composition":
            continue
        for s in spec["sources"]:
            for sex in (s.get("by_sex") or {"계": {}}).keys():
                src = lookup._effective(s, sex)
                exp = _expected(src)
                lo, hi = lookup._years(src)
                label = f"{name} [{src['table']}{'' if sex == '계' else ', ' + sex}]"
                if src.get("national_only"):
                    got = lookup._fetch(src, ["00"], lo, hi)
                    pairs = [(v, exp.get(y)) for (y, _), v in got.items()]
                else:
                    # 서비스 지역 코드 ↔ 원본 지역 코드 (전국·시도만)
                    mapping = db_rows(
                        """select distinct region_code, c1_id from facts
                           where tbl_id = ? and (region_code = '00' or length(region_code) = 2)""",
                        [src["table"]],
                    )
                    to_raw = {m["region_code"]: m["c1_id"] for m in mapping}
                    age = [src["age_total"]] if src.get("age_dim") else None
                    got = lookup._fetch(src, list(to_raw), lo, hi, age)
                    pairs = [(v, exp.get((y, to_raw[c]))) for (y, c), v in got.items()]
                bad = [
                    (g, e)
                    for g, e in pairs
                    if (g is None) != (e is None or pd.isna(e))
                    or (g is not None and abs(g - e) > TOLERANCE * max(1, abs(e)))
                ]
                lines.append(
                    f"- {label}: {len(pairs)}개 비교, 불일치 {len(bad)}개"
                    + (f" (예: {bad[:2]})" if bad else "")
                )
    return lines


# ---- 3. 기간 ---------------------------------------------------------------------


def check_periods() -> list[str]:
    lines = []
    for c in db_rows("select tbl_id, start_year, end_year from catalog order by tbl_id"):
        df = raw(c["tbl_id"])
        lo, hi = int(df["year"].min()), int(df["year"].max())
        ok = (lo, hi) == (c["start_year"], c["end_year"])
        mark = "일치" if ok else "불일치"
        service = f"{c['start_year']}~{c['end_year']}"
        lines.append(f"- {c['tbl_id']}: 원본 {lo}~{hi}, 서비스 {service} {mark}")
    return lines


def main() -> None:
    sections = [
        ("1. 계산 지표 (주민등록인구, 전 연도·전 지역·성별 3)", check_population()),
        ("2. 조회 지표 (전국·시도, 전 연도)", check_lookup()),
        ("3. 수록 기간", check_periods()),
    ]
    problem = re.compile(r"불일치 [1-9]|어긋난 행 [1-9]|불일치$")
    bad = sum(1 for _, lines in sections for line in lines if problem.search(line))
    report = [f"# 원본 CSV ↔ 서비스 값 대조 {datetime.now():%Y-%m-%d %H:%M}", ""]
    report.append(f"문제 항목 {bad}개" + (" (모두 일치)" if bad == 0 else ""))
    for title, lines in sections:
        report += ["", f"## {title}", "", *lines]
    text = "\n".join(report)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"raw_check_{datetime.now():%Y%m%d_%H%M%S}.md"
    out.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
