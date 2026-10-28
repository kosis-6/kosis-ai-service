"""시트 골든셋 정답 재계산 채점: 서비스 코드 없이 pandas로 정답을 직접 계산해 답변 문장과 대조한다

eval_golden.py는 시트 문항에서 행동(답변/명확화/…)·표·출처만 채점해 숫자가 틀려도 통과한다.
여기서는 kosis.duckdb의 facts·population을 pandas로 읽어 문항마다 정답을 따로 계산하고
(app.services를 쓰지 않음), 채점 결과 CSV의 answer 문장에 정답 값·이름이 있는지 본다.

지표
  최종 정답률      문항 점수 평균 (1 / 부분 0.5 / 0)
  정상 문항 정답률 s01~s45 점수 평균
  예외 대응 정답률 s46~s60에서 기대 행동(allowed_action)을 한 비율
  환각률           답할 수 없는 문항(불가·명확화)에서 숫자를 내놓은 비율
  표 선택 정확도   eval_golden.py의 표 선택 결과
  조건 추출 정확도 지역·시점·항목·분류가 답변에 맞게 들어갔는지
  계산 정확도      조건·표가 맞은 문항 중 값까지 맞은 비율
  해석 고지율      시트 required_notice(성별·지표 해석·추계값 등)를 답변에 밝힌 비율
  자연어 품질      사람이 결과 CSV의 quality 칸(0~2점)을 채운 경우만 평균

문항 점수: 정답 값·이름이 모두 있으면 1, 절반 이상이거나 '매년 증가/감소'가 실제 추이와 어긋나면 0.5, 그 밖 0
예외 문항: 기대 행동 + 안내 문장이면 1, 기대 행동만 맞으면 0.5

실행: python -m scripts.eval_oracle data/eval/golden_r7_20261026_151907.csv
결과: data/eval/oracle_<입력 이름>.md (지표 요약 + 문항별 질문·답변·정답·채점) / .csv
"""

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

DB = Path("data/processed/kosis.duckdb")
GOLDEN = Path("eval/golden_sheet.csv")
OUT_DIR = Path("data/eval")

con = duckdb.connect(str(DB), read_only=True)
FACTS = con.execute(
    "select tbl_id, year, item_nm, c1_nm, c2_nm, c3_nm, region_code, value, scenario from facts"
).df()
POP = con.execute("select year, region_code, sex, age, is_open_age, age_label, population from population").df()
REGIONS = con.execute("select region_code, region_name, kind, sido_name, last_year from regions").df()
SIDO = REGIONS[REGIONS.kind == "시도"].set_index("region_code").region_name.to_dict()
SIDO.pop("12", None)  # 전남광주통합특별시: 수록 연도 없음


# ---------- pandas 조회 ----------


def facts(tbl: str, **eq: Any) -> pd.DataFrame:
    df = FACTS[FACTS.tbl_id == tbl]
    for col, want in eq.items():
        df = df[df[col].isin(want)] if isinstance(want, (list, tuple, set)) else df[df[col] == want]
    return df


def one(tbl: str, **eq: Any) -> float:
    df = facts(tbl, **eq).drop_duplicates(["year", "item_nm", "c1_nm", "c2_nm", "c3_nm", "region_code"])
    assert len(df) == 1, (tbl, eq, len(df))
    return float(df.value.iloc[0])


def by_region(tbl: str, codes: list[str], **eq: Any) -> dict[str, float]:
    """지역 코드 → 값 (지역 이름으로 바꿔서 돌려줌)"""
    df = facts(tbl, region_code=codes, **eq).drop_duplicates("region_code")
    return {name_of(c): float(v) for c, v in zip(df.region_code, df.value)}


def series(tbl: str, **eq: Any) -> dict[int, float]:
    df = facts(tbl, **eq).drop_duplicates("year").sort_values("year")
    return {int(y): float(v) for y, v in zip(df.year, df.value)}


def pop(year: int, code: str, sex: str = "계", ages: tuple[int, int | None] | None = None) -> float:
    df = POP[(POP.year == year) & (POP.region_code == code) & (POP.sex == sex)]
    if ages is None:
        return float(df[df.age_label == "계"].population.sum())
    lo, hi = ages
    df = df[df.age.notna()]
    df = df[(df.age >= lo) & ((df.age <= hi) if hi is not None else True)]
    return float(df.population.sum())


def name_of(code: str) -> str:
    row = REGIONS[REGIONS.region_code == code]
    if code == "00":
        return "전국"
    name = row.region_name.iloc[0]
    return name if len(code) == 2 else f"{row.sido_name.iloc[0]} {name}"


def code_of(name: str, sido: str | None = None) -> str:
    df = REGIONS[(REGIONS.region_name == name) & (REGIONS.kind.isin(["시도", "시군구"]))]
    if sido:
        df = df[df.sido_name == sido]
    assert len(df) == 1, (name, sido, len(df))
    return df.region_code.iloc[0]


def gu_codes(sido_code: str, year: int) -> list[str]:
    df = REGIONS[(REGIONS.kind == "시군구") & REGIONS.region_code.str.startswith(sido_code)]
    return df[df.last_year >= year].region_code.tolist()


def pct(a: float, b: float) -> float:
    return a / b * 100


# ---------- 문항별 정답 ----------
# values: (설명, 값, 소수 자릿수) 답변 본문에 있어야 함 / names: 첫 문단에 있어야 할 이름·연도
# cond: 조건(지역·시점·항목·분류)이 답변에 드러나는 말 / notice: required_notice 확인 말 (re 패턴)
# trends: '매년 증가/감소' 문장과 대조할 연도별 값 / basis: 계산 방법

SEJONG, SEOUL, BUSAN, DAEGU, INCHEON, GWANGJU, DAEJEON, ULSAN = "36", "11", "26", "27", "28", "29", "30", "31"
GYEONGGI, CHUNGBUK, CHUNGNAM, JEONNAM, GANGWON, JEONBUK = "41", "43", "44", "46", "51", "52"
METRO5 = [BUSAN, DAEGU, GWANGJU, DAEJEON, ULSAN]
THIRD_PLUS = ["3아", "4아", "5아", "6아", "7아", "8아이상"]
BIRTH = "출생건수 (명)"
MARRIAGE = "혼인건수 (건)"


def s01():
    v = one("DT_1B8000H", year=2025, region_code=INCHEON, item_nm=MARRIAGE)
    return dict(values=[("인천 혼인 건수", v, 0)], cond=["인천광역시", "2025", "혼인 건수"],
                basis="DT_1B8000H 혼인건수, 인천 2025")


def s02():
    codes = gu_codes(SEOUL, 2023)
    vals = {name_of(c): pop(2023, c, ages=(20, 29)) for c in codes}
    top, low = max(vals, key=vals.get), min(vals, key=vals.get)
    return dict(values=[(k, v, 0) for k, v in vals.items()], names=[],
                cond=["서울특별시", "2023", "20대"],
                basis=f"주민등록인구 2023 서울 {len(codes)}개 구, 20~29세 합 (최대 {top}, 최소 {low})")


def s03():
    v = one("INH_1B8000F_01", year=2025, c1_nm="합계출산율(명)")
    return dict(values=[("합계출산율", v, 3)], cond=["전국", "2025", "합계출산율"],
                basis="INH_1B8000F_01 합계출산율 2025")


def s04():
    v = one("DT_1BB0002", year=2024, region_code=BUSAN, item_nm="다문화 혼인 건수(건)")
    return dict(values=[("부산 다문화 혼인", v, 0)], cond=["부산광역시", "2024", "다문화 혼인"],
                basis="DT_1BB0002 다문화 혼인 건수, 부산 2024")


def s05():
    c = code_of("시흥시", "경기도")
    v = one("DT_1BB0006", year=2021, region_code=c, item_nm="다문화 출생아수(명)")
    return dict(values=[("시흥시 다문화 출생아", v, 0)], cond=["시흥시", "2021", "다문화 출생"],
                basis="DT_1BB0006 다문화 출생아수, 경기 시흥시 2021")


def s06():
    h = one("DT_1B83A05", year=2022, region_code=DAEGU, item_nm="남편")
    w = one("DT_1B83A05", year=2022, region_code=DAEGU, item_nm="아내")
    return dict(values=[("남편", h, 2), ("아내", w, 2)], cond=["대구광역시", "2022", "초혼연령"],
                notice=["남편", "아내"], basis="DT_1B83A05 평균초혼연령 남편·아내, 대구 2022")


def s07():
    c = code_of("강남구", "서울특별시")
    v = one("DT_1B81A23", year=2020, region_code=c, item_nm="합계출산율")
    return dict(values=[("강남구 합계출산율", v, 3)], cond=["강남구", "2020", "합계출산율"],
                notice=["합계출산율"], basis="DT_1B81A23 합계출산율, 서울 강남구 2020 ('합계출생율' → 합계출산율)")


def s08():
    v = pop(2025, code_of("유성구", "대전광역시"), ages=(6, 11))
    return dict(values=[("유성구 6~11세", v, 0)], cond=["유성구", "2025", "6~11세"],
                basis="주민등록인구 2025 대전 유성구 6~11세 합")


def s09():
    codes = [DAEJEON, SEJONG, CHUNGBUK, CHUNGNAM]
    vals = by_region("DT_1BB0002", codes, year=2024, item_nm="다문화 혼인 건수(건)")
    return dict(values=[("합계", sum(vals.values()), 0)] + [(k, v, 0) for k, v in vals.items()],
                cond=["충청권", "2024", "다문화 혼인"], notice=list(vals),
                basis="DT_1BB0002 2024 대전+세종+충북+충남 다문화 혼인 건수 합")


def s10():
    v = facts("DT_1B81A12", year=2025, region_code=GYEONGGI, item_nm="계", c2_nm="계", c3_nm=THIRD_PLUS).value.sum()
    return dict(values=[("경기 셋째 이상", v, 0)], cond=["경기도", "2025", "셋째 이상"],
                basis="DT_1B81A12 2025 경기 성별 계·연령 계, 출산순위 3아~8아이상 합")


def s11():
    vals = by_region("DT_1B8000H", [SEOUL, INCHEON, GYEONGGI], year=2025, item_nm=BIRTH)
    return dict(values=[("수도권 합", sum(vals.values()), 0)] + [(k, v, 0) for k, v in vals.items()],
                cond=["수도권", "2025", "출생아"], basis="DT_1B8000H 출생건수 2025 서울+인천+경기")


def s12():
    a = pop(2025, code_of("해운대구", "부산광역시"), ages=(65, None))
    b = pop(2025, code_of("수영구", "부산광역시"), ages=(65, None))
    return dict(values=[("합계", a + b, 0), ("해운대구", a, 0), ("수영구", b, 0)],
                cond=["해운대구", "수영구", "2025", "65세 이상"], notice=["12월"],
                basis="주민등록인구 2025 부산 해운대구·수영구 65세 이상 합 (12월 31일 기준)")


def s13():
    c = code_of("안산시", "경기도")
    s = series("DT_1BB0006", region_code=c, item_nm="다문화 출생아수(명)")
    last = max(s)
    years = list(range(last - 4, last + 1))
    return dict(values=[("5년 합", sum(s[y] for y in years), 0)] + [(f"{y}", s[y], 0) for y in years],
                cond=["안산시", f"{years[0]}~{years[-1]}", "다문화 출생"], notice=[f"{years[0]}~{years[-1]}"],
                basis=f"DT_1BB0006 안산시 다문화 출생아수, 수록 마지막 5년({years[0]}~{years[-1]}) 합")


def s14():
    v = one("DT_1B8000H", year=2025, region_code=JEONNAM, item_nm="자연증가건수 (명)")
    return dict(values=[("자연증가(절댓값)", v, 0)], cond=["전라남도", "2025"],
                basis="DT_1B8000H 자연증가건수 전남 2025 (음수 = 사망이 더 많음)")


def s15():
    s = series("INH_1B8000F_01", c1_nm="출생아수(명)")
    return dict(values=[("2024", s[2024], 0), ("2025", s[2025], 0), ("증가", s[2025] - s[2024], 0),
                        ("증감률", pct(s[2025] - s[2024], s[2024]), 2)],
                cond=["2024", "2025", "출생아"], basis="INH_1B8000F_01 출생아수 2024 → 2025")


def s16():
    h = series("DT_1B83A05", region_code=GYEONGGI, item_nm="남편")
    w = series("DT_1B83A05", region_code=GYEONGGI, item_nm="아내")
    ys = range(2021, 2026)
    return dict(values=[("남편 2021", h[2021], 2), ("남편 2025", h[2025], 2), ("아내 2021", w[2021], 2), ("아내 2025", w[2025], 2)],
                cond=["경기도", "2021", "2025", "초혼연령"], notice=["2021", "2025", "남편", "아내"],
                trends={"남편": {y: h[y] for y in ys}, "아내": {y: w[y] for y in ys}},
                basis="DT_1B83A05 경기 평균초혼연령 남편·아내 2021~2025 (최근 5년)")


def s17():
    s = series("DT_1B8000H", region_code=GWANGJU, item_nm="조혼인율 (천명당)")
    return dict(values=[("2015", s[2015], 1), ("2020", s[2020], 1), ("증감률", pct(s[2020] - s[2015], s[2015]), 2)],
                cond=["광주광역시", "2015", "2020"], notice=["조혼인율|일반혼인율"],
                trends={"조혼인율": {y: s[y] for y in range(2015, 2021)}},
                basis="DT_1B8000H 조혼인율 광주 2015 → 2020 증감률")


def s18():
    s = {y: v for y, v in series("DT_1B81A21", region_code=JEONBUK, item_nm="합계출산율").items() if y >= 1990}
    y = min(s, key=s.get)
    return dict(values=[("최저값", s[y], 2)], names=[f"{y}년"], cond=["전북특별자치도", "합계출산율"],
                basis=f"DT_1B81A21 전북 합계출산율 {min(s)}~{max(s)} 중 최저 (1990~1992 자료 없음)")


def s19():
    s = series("DT_1B26002", region_code=SEJONG, c2_nm="계", c3_nm="계")
    years, streak = sorted(s), 0
    for y in years:
        if s[y] <= 0:
            break
        streak += 1
    return dict(values=[], names=[f"{streak}년"], cond=["세종특별자치시", "순"], notice=[str(years[0])],
                basis=f"DT_1B26002 세종 순이동(계) {years[0]}년부터 연속 양수인 해 수 = {streak}")


def s20():
    s = series("DT_1B81A20", region_code="00", item_nm="첫째 아")
    return dict(values=[("2015", s[2015], 2), ("2025", s[2025], 2), ("연평균", (s[2025] - s[2015]) / 10, 1)],
                cond=["2015", "2025", "첫째"], notice=["전국"],
                trends={"첫째아": {y: s[y] for y in range(2015, 2026)}},
                basis="DT_1B81A20 전국 첫째 아 평균 출산연령 (2025-2015) ÷ 10")


def s21():
    s = {y: v for y, v in series("DT_1B81A21", region_code=SEOUL, item_nm="합계출산율").items() if y >= 2000}
    ys = sorted(s)
    rebound = max(ys[i] for i in range(2, len(ys)) if s[ys[i]] > s[ys[i - 1]] < s[ys[i - 2]])
    return dict(values=[], names=[f"{rebound}년"], cond=["서울특별시", "합계출산율"], notice=["기준"],
                basis=f"DT_1B81A21 서울 합계출산율 2000~: 감소 후 다시 오른 가장 최근 해 = {rebound}")


def s22():
    vals = by_region("DT_1B8000H", [BUSAN, DAEGU], year=2025, item_nm=MARRIAGE)
    top = max(vals, key=vals.get)
    return dict(values=[(k, v, 0) for k, v in vals.items()] + [("차이", abs(vals["부산광역시"] - vals["대구광역시"]), 0)],
                names=[top], cond=["부산광역시", "대구광역시", "2025"], notice=["건수|조혼인율"],
                basis="DT_1B8000H 혼인건수 2025 부산 vs 대구")


def s23():
    vals = by_region("DT_1B81A20", [SEOUL, JEONNAM], year=2025, item_nm="첫째 아")
    top = max(vals, key=vals.get)
    return dict(values=[(k, v, 2) for k, v in vals.items()] + [("차이", abs(vals["서울특별시"] - vals["전라남도"]), 2)],
                names=[top], cond=["서울특별시", "전라남도", "2025", "첫째"],
                basis="DT_1B81A20 첫째 아 평균 출산연령 2025 서울 vs 전남")


def s24():
    h = one("DT_1B83A34", year=2024, region_code=GANGWON, item_nm="남편")
    w = one("DT_1B83A34", year=2024, region_code=GANGWON, item_nm="아내")
    return dict(values=[("남자", h, 1), ("여자", w, 1)], cond=["강원특별자치도", "2024", "일반혼인율"],
                basis="DT_1B83A34 일반혼인율 남편·아내, 강원 2024")


def s25():
    a = one("DT_1BB0006", year=2024, region_code=code_of("안산시", "경기도"), item_nm="다문화 출생아수(명)")
    b = one("DT_1BB0006", year=2024, region_code=code_of("수원시", "경기도"), item_nm="다문화 출생아수(명)")
    return dict(values=[("안산시", a, 0), ("수원시", b, 0), ("배수", a / b, 2)], cond=["안산시", "수원시", "2024"],
                notice=["적" if a < b else "많"], basis="DT_1BB0006 다문화 출생아수 2024 안산 ÷ 수원")


def s26():
    vals = by_region("DT_1BB0002", METRO5, year=2024, item_nm="다문화 혼인 건수(건)")
    top, low = max(vals, key=vals.get), min(vals, key=vals.get)
    return dict(values=[(k, v, 0) for k, v in vals.items()], names=[top, low], cond=list(vals) + ["2024"],
                notice=list(vals), basis="DT_1BB0002 2024 5대 광역시(부산·대구·광주·대전·울산) 다문화 혼인 건수")


def s27():
    out = {}
    for c in [GYEONGGI, CHUNGNAM]:
        total = one("TX_11025_A000_A", year=2024, region_code=c, c2_nm="합계", c3_nm="합계")
        people = one("TX_11025_A000_A", year=2024, region_code=c, c2_nm="총인구", c3_nm="합계")
        out[name_of(c)] = pct(total, people)
    return dict(values=[(k, v, 2) for k, v in out.items()], names=[max(out, key=out.get)],
                cond=["경기도", "충청남도", "2024", "외국인주민 비율"],
                basis="TX_11025_A000_A 2024 외국인주민 합계 ÷ 총인구 × 100")


def s28():
    vals = []
    for age in ["20대", "30대"]:
        df = facts("DT_1B26B24", year=2024, region_code=SEOUL, c2_nm=age)
        total = float(df[df.c3_nm == "계"].value.iloc[0])
        shares = {r.c3_nm: pct(r.value, total) for r in df[df.c3_nm != "계"].itertuples()}
        vals += [(f"{age} {k}", v, 2) for k, v in sorted(shares.items(), key=lambda kv: -kv[1])[:3]]
    return dict(values=vals, cond=["서울특별시", "2024", "20대", "30대"],
                basis="DT_1B26B24 2024 서울 20대·30대 전입 사유별 ÷ 계 (상위 3개씩)")


def s29():
    vals = by_region("DT_1B81A21", [ULSAN, "00"], year=2020, item_nm="합계출산율")
    return dict(values=[(k, v, 3) for k, v in vals.items()], names=[max(vals, key=vals.get)],
                cond=["울산광역시", "전국", "2020"], basis="DT_1B81A21 합계출산율 2020 울산 vs 전국")


def s30():
    vals = by_region("DT_1B81A21", list(SIDO), year=2025, item_nm="합계출산율")
    top3 = sorted(vals, key=vals.get, reverse=True)[:3]
    return dict(values=[(k, vals[k], 3) for k in top3], names=top3, cond=["2025", "합계출산율"],
                basis="DT_1B81A21 합계출산율 2025 시도 17곳 상위 3")


def s31():
    vals = by_region("DT_1B81A23", gu_codes(SEOUL, 2025), year=2025, item_nm="합계출산율")
    low = min(vals, key=vals.get)
    return dict(values=[(low, vals[low], 3)], names=[low.split()[-1]], cond=["서울특별시", "2025", "합계출산율"],
                basis=f"DT_1B81A23 합계출산율 2025 서울 {len(vals)}개 구 중 최저")


def s32():
    codes = REGIONS[(REGIONS.kind == "시군구") & (REGIONS.last_year >= 2025)].region_code.tolist()
    vals = by_region("DT_1B26002", codes, year=2025, c2_nm="계", c3_nm="계")
    low = min(vals, key=vals.get)
    return dict(values=[(low, vals[low], 0)], names=[low.split()[-1]], cond=["2025", "시군구"], notice=["2025"],
                basis=f"DT_1B26002 순이동(계) 2025 시군구 {len(vals)}곳 중 최저 ('작년' = 2025)")


def s33():
    vals = by_region("DT_1B83A05", list(SIDO), year=2025, item_nm="아내")
    top, low = max(vals, key=vals.get), min(vals, key=vals.get)
    return dict(values=[(top, vals[top], 2), (low, vals[low], 2)], names=[top, low], cond=["2025", "아내|여자"],
                basis="DT_1B83A05 평균초혼연령(아내) 2025 시도 최고·최저")


def s34():
    df = facts("DT_1B81A12", year=2025, region_code=list(SIDO), item_nm="계", c2_nm="계", c3_nm=THIRD_PLUS)
    vals = df.groupby("region_code").value.sum()
    top = vals.idxmax()
    return dict(values=[(name_of(top), float(vals[top]), 0)], names=[name_of(top)], cond=["2025", "셋째 이상"],
                basis="DT_1B81A12 2025 시도별 3아~8아이상 합 최대")


def s35():
    groups = {"합계", "기타", "미상", "동남아시아", "동북아시아", "서남아시아", "중앙아시아", "아시아(기타)",
              "유럽", "아프리카", "중남미", "오세아니아"}
    df = facts("TX_11025_A017_A", year=2024, region_code="00").drop_duplicates("c2_nm")
    total = float(df[df.c2_nm == "합계"].value.iloc[0])
    top5 = df[~df.c2_nm.isin(groups)].nlargest(5, "value")
    return dict(values=[(r.c2_nm, r.value, 0) for r in top5.itertuples()] + [("합계", total, 0)],
                names=top5.c2_nm.tolist(), cond=["2024"],
                basis="TX_11025_A017_A 2024 전국 이전 국적별(대륙 묶음·기타·미상 제외) 상위 5")


def s36():
    s = {y: v for y, v in series("INH_1B8000F_01", c1_nm="출생아수(명)").items() if y >= 1970}
    low3 = sorted(s, key=s.get)[:3]
    return dict(values=[(f"{y}", s[y], 0) for y in low3], names=[f"{y}년" for y in low3], cond=["1970", "출생아"],
                basis="INH_1B8000F_01 출생아수 1970~ 가장 적은 3개 연도")


def s37():
    cities = REGIONS[(REGIONS.kind == "시군구") & (REGIONS.last_year >= 2024) & REGIONS.region_name.str.endswith("시")
                     & ~REGIONS.sido_name.str.endswith(("특별시", "광역시"))]
    vals = by_region("DT_1BB0006", cities.region_code.tolist(), year=2024, item_nm="다문화 출생아수(명)")
    ranked = sorted(vals, key=vals.get, reverse=True)
    rank = ranked.index("경기도 안산시") + 1
    return dict(values=[("안산시", vals["경기도 안산시"], 0)], names=[f"{rank}위"], cond=["안산시", "2024"],
                notice=[r"시 \d+곳"], basis=f"DT_1BB0006 2024 도 소속 시 {len(vals)}곳 다문화 출생아수 순위")


def s38():
    out = {}
    for c in gu_codes(SEOUL, 2024):
        married = facts("DT_1MR2060", year=2024, region_code=c, item_nm="여자_유배우", c2_nm="합계")
        total = facts("DT_1MR2060", year=2024, region_code=c, item_nm="여자_계", c2_nm="합계")
        if len(married) and len(total):
            out[name_of(c)] = pct(married.value.iloc[0], total.value.iloc[0])
    return dict(values=[(k, v, 2) for k, v in out.items()], cond=["서울특별시", "2024", "유배우"],
                notice=[r"1[58]세 이상"],
                basis=f"DT_1MR2060 2024 서울 {len(out)}개 구 여자_유배우 ÷ 여자_계 (표가 18세 이상만 수록)")


def s39():
    df = facts("DT_1B81A12", year=2025, region_code="00", item_nm="계", c2_nm="계").drop_duplicates("c3_nm")
    v = df.set_index("c3_nm").value
    return dict(values=[("첫째 비율", pct(v["1아"], v["총계"]), 2), ("첫째", v["1아"], 0)], cond=["2025", "첫째"],
                notice=["전국"], basis="DT_1B81A12 2025 전국 1아 ÷ 총계 × 100")


def s40():
    kinds = ["외국인근로자", "결혼이민자", "유학생", "외국국적동포", "기타외국인", "한국국적을 취득한 자", "외국인주민자녀(출생)"]
    df = facts("TX_11025_A000_A", year=2024, region_code=GYEONGGI, c3_nm="합계").drop_duplicates("c2_nm").set_index("c2_nm").value
    return dict(values=[(k, pct(df[k], df["합계"]), 2) for k in kinds], cond=["경기도", "2024", "유형"],
                basis="TX_11025_A000_A 2024 경기 유형별(국적 미취득 5종 + 국적 취득자 + 자녀) ÷ 합계")


def s41():
    df = facts("DT_1B26B24", year=2024, region_code=BUSAN, c2_nm="계").drop_duplicates("c3_nm").set_index("c3_nm").value
    return dict(values=[("직업 비율", pct(df["직업"], df["계"]), 2)], cond=["부산광역시", "2024"], notice=["직업"],
                basis="DT_1B26B24 2024 부산 연령 계, 직업 ÷ 계 ('일자리' → 직업)")


def s42():
    v = one("DT_1BB0006", year=2024, region_code=code_of("안산시", "경기도"), item_nm="다문화 출생비중(%)")
    return dict(values=[("다문화 출생 비중", v, 1)], cond=["안산시", "2024"], basis="DT_1BB0006 다문화 출생비중, 안산 2024")


def s43():
    c = code_of("영도구", "부산광역시")
    return dict(values=[("고령인구 비율", pct(pop(2025, c, ages=(65, None)), pop(2025, c)), 2)],
                cond=["영도구", "2025", "고령인구 비율"], basis="주민등록인구 2025 영도구 65세 이상 ÷ 계")


def s44():
    seoul = one("DT_110025_A041_A", year=2024, region_code=SEOUL, c2_nm="합계", c3_nm="계")
    total = one("DT_110025_A041_A", year=2024, region_code="00", c2_nm="합계", c3_nm="계")
    return dict(values=[("서울", seoul, 0), ("전국", total, 0), ("비중", pct(seoul, total), 2)], cond=["서울특별시", "2024"],
                basis="DT_110025_A041_A 2024 외국인주민 자녀 서울 ÷ 전국")


def s45():
    ages = ["30~34세", "35~39세"]
    single = facts("DT_1MR2060", year=2025, region_code=SEOUL, item_nm="남자_미혼", c2_nm=ages).value.sum()
    total = facts("DT_1MR2060", year=2025, region_code=SEOUL, item_nm="남자_계", c2_nm=ages).value.sum()
    return dict(values=[("미혼 비율", pct(single, total), 2)], cond=["서울특별시", "2025", "30대", "남"],
                basis="DT_1MR2060 2025 서울 남자 30~39세 미혼 ÷ 계")


def s51():
    return dict(values=[], cond=[], notice=["판단할 수 없"], basis="인과 질문: 수치만 제시하고 인과 판단 불가를 밝히면 정답")


def s56():
    v = one("DT_1BPB002", year=2040, c2_nm="서울특별시", c3_nm="총인구(명)", scenario="중위 추계")
    return dict(values=[("2040 서울 추계", v, 0)], cond=["서울특별시", "2040"], notice=["추계", "중위"],
                basis="DT_1BPB002 장래인구추계(중위) 서울 2040 총인구")


def s58():
    return dict(values=[("군위군 2020", pop(2020, code_of("군위군", "경상북도")), 0)], cond=["군위군", "2020"],
                notice=["경상북도"], basis="주민등록인구 2020 경북 군위군 (2023년 대구 편입 전)")


def s60():
    m = one("DT_1B8000H", year=2025, region_code=SEOUL, item_nm=MARRIAGE)
    d = one("DT_1B8000H", year=2025, region_code=SEOUL, item_nm="이혼건수 (건)")
    return dict(values=[("이혼÷혼인", pct(d, m), 2)], cond=["서울특별시", "2025"], notice=["아니"],
                basis="DT_1B8000H 2025 서울 이혼건수 ÷ 혼인건수 (이혼율이 아님을 밝혀야 함)")


# 답할 수 없는 문항: 기대 행동 + 안내 문장
EXCEPTIONS = {
    "s46": ["지원 지표"], "s47": ["인구통계"], "s48": ["가구"], "s49": ["연수구"], "s50": ["지역"],
    "s52": ["광주광역시", "광주시"], "s53": ["경상남도 고성군", "강원특별자치도 고성군"], "s54": ["2024"],
    "s55": ["2022"], "s57": ["2052"], "s59": ["충청북도", "충청남도"],
}
ORACLES = {name: fn for name, fn in globals().items() if re.fullmatch(r"s\d\d", name)}


# ---------- 채점 ----------

NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def body(answer: str) -> str:
    return "\n".join(l for l in answer.splitlines() if not l.startswith(("출처", "통계표")))


def numbers(text: str) -> list[float]:
    return [float(n.replace(",", "")) for n in NUMBER.findall(text)]


def has_value(nums: list[float], value: float, digits: int) -> bool:
    tol = 0.5 * 10**-digits + 1e-9
    return any(abs(abs(n) - abs(round(value, digits))) <= tol for n in nums)


def has_word(text: str, pattern: str) -> bool:
    return re.search(pattern, text) is not None


def trend_conflict(text: str, trends: dict[str, dict[int, float]]) -> str:
    """'매년 증가/감소'라고 했는데 실제로 그렇지 않은 계열"""
    for word, ok in [("매년 증가", lambda a, b: b > a), ("매년 감소", lambda a, b: b < a)]:
        for sentence in (x for x in re.split(r"(?<=다\.)\s*", text) if word in x):
            # 문장에 대상(예: 아내)이 있으면 그 계열만, 없으면 모든 계열과 대조
            named = {k: v for k, v in trends.items() if k in sentence} or trends
            for label, s in named.items():
                ys = sorted(s)
                bad = [y for a, y in zip(ys, ys[1:]) if not ok(s[a], s[y])]
                if bad:
                    return f"'{word}'라고 했지만 {label} {', '.join(map(str, bad))}년은 아님"
    return ""


def hallucinated(answer: str) -> bool:
    """연도·날짜가 아닌 숫자(값)를 내놓았는지"""
    text = "\n".join(l for l in body(answer).splitlines() if not l.startswith("※"))  # 계산식 안내
    text = re.sub(r"\d+\s*(?:년|월|일)|'[^']*'", "", text)  # 날짜, 되풀이한 질문 용어('1인 가구')
    return any(not (n.is_integer() and 1900 <= n <= 2100) for n in numbers(text))


def grade_case(row: dict[str, str], gold: dict[str, str]) -> dict[str, Any]:
    qid, answer = row["id"], row.get("answer") or ""
    text = body(answer)
    first = text.split("\n\n")[0]
    action = row.get("action", "")
    allowed = {a.strip() for a in gold["allowed_action"].split(",")}
    out: dict[str, Any] = {"id": qid, "question": row["question"], "answer": answer, "action": action,
                           "expected_action": gold["allowed_action"], "action_ok": action in allowed,
                           "table_ok": {"True": True, "False": False}.get(row.get("표 선택", "")),
                           "tables": row.get("tables", ""), "quality": row.get("quality", "")}

    if qid in EXCEPTIONS:
        notice_ok = all(has_word(answer, p) for p in EXCEPTIONS[qid])
        # 명확화 대신 답해도 되는 문항(명확화, 답변)에서 답한 숫자는 지어낸 것이 아님
        halluc = hallucinated(answer) and not (action == "답변" and "답변" in allowed)
        score = (1.0 if notice_ok else 0.5) if out["action_ok"] and not halluc else 0.0
        out.update(kind="예외", basis="기대 행동: " + gold["allowed_action"], expect="안내: " + ", ".join(EXCEPTIONS[qid]),
                   notice_ok=notice_ok, hallucinated=halluc, score=score,
                   reason="" if score == 1 else ("숫자를 지어냄" if halluc else "기대 행동 아님" if not out["action_ok"] else "안내 문장 없음"))
        return out

    spec = ORACLES[qid]()
    nums = numbers(text)
    hits = [(label, v, d, has_value(nums, v, d)) for label, v, d in spec.get("values", [])]
    name_hits = [(n, n in first) for n in spec.get("names", [])]
    cond_hits = [(c, has_word(text, c)) for c in spec.get("cond", [])]
    notice = spec.get("notice", [])
    conflict = trend_conflict(text, spec.get("trends", {}))

    checks = [h for *_, h in hits] + [h for _, h in name_hits]
    if not out["action_ok"]:
        score, reason = 0.0, f"기대 행동 아님 ({action})"
    elif checks and all(checks) and not conflict:
        score, reason = 1.0, ""
    elif conflict or (checks and sum(checks) >= len(checks) / 2):
        missing = [str(l) for l, _, _, h in hits if not h] + [n for n, h in name_hits if not h]
        score, reason = 0.5, conflict or f"빠진 값: {', '.join(missing[:5])}"
    elif not checks:
        score, reason = (1.0, "") if all(has_word(answer, p) for p in notice) else (0.5, "안내 문장 없음")
    else:
        missing = [str(l) for l, _, _, h in hits if not h] + [n for n, h in name_hits if not h]
        score, reason = 0.0, f"빠진 값: {', '.join(missing[:5])}"

    out.update(kind="정상" if int(qid[1:]) <= 45 else "예외", basis=spec["basis"],
               expect="; ".join([f"{l}={_fmt(v, d)}{'' if h else ' ✗'}" for l, v, d, h in hits]
                                + [f"{n}{'' if h else ' ✗'}" for n, h in name_hits]),
               cond_ok=all(h for _, h in cond_hits) if cond_hits else None,
               cond_missing=[c for c, h in cond_hits if not h],
               values_ok=(all(checks) and not conflict) if checks else None,
               notice_ok=all(has_word(answer, p) for p in notice) if notice else None,
               notice_missing=[p for p in notice if not has_word(answer, p)],
               conflict=conflict, hallucinated=False, score=score, reason=reason)
    return out


def _fmt(v: float, d: int) -> str:
    return f"{v:,.{d}f}"


# ---------- 요약 ----------


def rate(items: list[bool]) -> str:
    return f"{sum(items) / len(items) * 100:.1f}% ({sum(items)}/{len(items)})" if items else "-"


def mean(items: list[float]) -> str:
    return f"{sum(items) / len(items) * 100:.1f}% ({sum(items):g}/{len(items)})" if items else "-"


def metrics(rows: list[dict[str, Any]], golds: dict[str, dict[str, str]]) -> list[tuple[str, str, str]]:
    normal = [r for r in rows if int(r["id"][1:]) <= 45]
    tail = [r for r in rows if int(r["id"][1:]) > 45]
    unanswerable = [r for r in rows if golds[r["id"]]["answerable"] in ("불가", "명확화")]
    data_rows = [r for r in rows if r.get("cond_ok") is not None]
    calc_rows = [r for r in data_rows if r["cond_ok"] and r["table_ok"] is not False and r["values_ok"] is not None]
    quality = [int(r["quality"]) for r in rows if str(r.get("quality", "")).strip().isdigit()]
    return [
        ("최종 정답률", mean([r["score"] for r in rows]), "채점한 문항의 최종 점수 평균 (부분 정답 0.5 반영)"),
        ("정상 문항 정답률", mean([r["score"] for r in normal]), "데이터로 답해야 하는 s01~s45 점수 평균"),
        ("예외 대응 정답률", rate([r["action_ok"] for r in tail]), "s46~s60에서 기대한 행동을 한 비율"),
        ("환각률", rate([r["hallucinated"] for r in unanswerable]), "답할 수 없는 문항에서 숫자를 지어낸 비율"),
        ("표 선택 정확도", rate([r["table_ok"] for r in rows if r["table_ok"] is not None]), "정답을 찾을 수 있는 표를 고른 비율"),
        ("조건 추출 정확도", rate([r["cond_ok"] for r in data_rows]), "지역·시점·항목·분류가 답변에 맞게 들어간 비율"),
        ("계산 정확도", rate([r["values_ok"] for r in calc_rows]), "조건·표가 맞은 문항 중 값까지 맞은 비율"),
        ("해석 고지율", rate([r["notice_ok"] for r in rows if r.get("notice_ok") is not None]), "필수 고지를 답변에 밝힌 비율"),
        ("자연어 품질 점수", f"{sum(quality) / len(quality):.2f} / 2 ({len(quality)}문항)" if quality else "미채점",
         "사람이 결과 CSV quality 칸(0~2)을 채운 문항 평균"),
    ]


def report(src: Path, rows: list[dict[str, Any]], golds: dict[str, dict[str, str]]) -> str:
    mark = {True: "○", False: "✗", None: "-"}
    lines = [f"# 정답 재계산 채점: {src.name}", "", f"- DB: {DB} (pandas로 직접 계산, 서비스 코드 미사용)",
             f"- 문항: {len(rows)}개", "", "## 지표", "", "| 지표 | 값 | 정의 |", "|---|---|---|"]
    lines += [f"| {n} | {v} | {d} |" for n, v, d in metrics(rows, golds)]
    lines += ["", "## 문항별 요약", "", "| id | 점수 | 행동 | 표 | 조건 | 값 | 고지 | 사유 |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['id']} | {r['score']:g} | {mark[r['action_ok']]} | {mark[r['table_ok']]} | "
                     f"{mark[r.get('cond_ok')]} | {mark[r.get('values_ok')]} | {mark[r.get('notice_ok')]} | {r['reason']} |")
    lines += ["", "## 문항별 상세 (질문 → 서비스 답변 → pandas 정답 → 채점)", ""]
    for r in rows:
        lines += ["---", "", f"### {r['id']} · 점수 {r['score']:g}", "", f"**질문** {r['question']}", "",
                  "**서비스 답변**", "", "```text", (r["answer"] or "(답변 없음)").rstrip(), "```", "",
                  f"**pandas 정답** {r['basis']}", "", f"- {r['expect'] or '-'}", "", "**채점**", "",
                  f"- 행동: {r['action']} (기대 {r['expected_action']}) {mark[r['action_ok']]}",
                  f"- 표 선택: {r['tables'] or '-'} {mark[r['table_ok']]}"]
        if r.get("cond_ok") is not None:
            lines.append(f"- 조건 추출: {mark[r['cond_ok']]}" + (f" (빠짐: {', '.join(r['cond_missing'])})" if r["cond_missing"] else ""))
            lines.append(f"- 값: {mark[r['values_ok']]}" + (f" — {r['conflict']}" if r["conflict"] else ""))
        if r.get("notice_ok") is not None:
            lines.append(f"- 해석 고지: {mark[r['notice_ok']]}" + (f" (빠짐: {', '.join(r.get('notice_missing', []))})" if r.get("notice_missing") else ""))
        if r["kind"] == "예외" and r["id"] in EXCEPTIONS:
            lines.append(f"- 숫자 지어냄: {'예' if r['hallucinated'] else '아니오'}")
        lines += [f"- **점수 {r['score']:g}**" + (f" — {r['reason']}" if r["reason"] else ""), ""]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="시트 골든셋 정답 재계산 채점")
    parser.add_argument("result", type=Path, help="eval_golden.py 결과 CSV (시트 60문항)")
    args = parser.parse_args()

    golds = {r["id"]: r for r in csv.DictReader(GOLDEN.open(encoding="utf-8-sig"))}
    results = [r for r in csv.DictReader(args.result.open(encoding="utf-8-sig")) if r["id"] in golds]
    rows = [grade_case(r, golds[r["id"]]) for r in results]

    stem = OUT_DIR / f"oracle_{args.result.stem}"
    stem.with_suffix(".md").write_text(report(args.result, rows, golds), encoding="utf-8")
    fields = ["id", "kind", "score", "action_ok", "table_ok", "cond_ok", "values_ok", "notice_ok", "hallucinated",
              "reason", "question", "basis", "expect", "answer"]
    with stem.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    for name, value, _ in metrics(rows, golds):
        print(f"{name:<10} {value}")
    print(f"\n저장: {stem}.md, {stem}.csv")


if __name__ == "__main__":
    main()
