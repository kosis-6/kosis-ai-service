"""KOSIS 수집 CSV → 정제된 DuckDB 생성

입력: data/raw/kosis_zip/processed/*.csv, *.meta.json
출력: data/processed/kosis.duckdb

만들어지는 테이블
  catalog         통계표 목록 (수록 기간, 분류, 추계표 여부)
  regions         지역 코드 계층 (전국/시도/시군구/일반구/출장소, 존속 기간)
  region_aliases  지역 별칭 → 지역 코드 (질문 속 지역명 해석용)
  region_changes  행정구역 변경 이력 (답변 안내 문구용)
  facts           통계표 20개를 합친 세로형 테이블 (지역 코드 통일, 결측·단위·추계 구분)
  population      1세별 주민등록인구 (연령·성별 분석용)

실행: python pipeline/preprocess.py   (가상환경 활성화 후, README 참고. 보통은 run_all.py로 실행)
"""

import json
import re
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "kosis_zip" / "processed"
OUT_PATH = ROOT / "data" / "processed" / "kosis.duckdb"

ORG_NAMES = {"101": "국가데이터처 (구 통계청)", "110": "행정안전부"}

# 장래인구추계(2022년 기준): 2022년까지는 기준인구, 2023년부터가 추계값
PROJECTION_TABLES = {"DT_1BPB002", "DT_1BPB003", "DT_1BPB101"}
PROJECTION_BASE_YEAR = 2022

# 지표 이름이 item_nm이 아니라 다른 분류 컬럼에 들어 있는 표
LABEL_COLUMN = {
    "INH_1B8000F_01": "c1_nm",
    "DT_1BPB002": "c3_nm",
    "DT_1BPB003": "c3_nm",
    "DT_1BPB101": "c3_nm",
}

# 지역 분류가 c1이 아닌 표 (None = 지역 분류 없는 전국 자료)
REGION_COLUMN = {
    "DT_1BPB002": "c2",
    "DT_1BPB003": "c2",
    "DT_1BPB101": "c2",
    "INH_1B8000F_01": None,
}

# 연령 분류 단위 (답변 불가 사례 2: 분류 기준 불일치 판단용)
AGE_UNIT = {
    "DT_1B04006": "1세",
    "DT_1BPB101": "1세",
    "DT_1B26002": "5세",
    "DT_1B81A12": "5세",
    "DT_1MR2060": "5세",
}

# KOSIS 결측 기호 (답변 불가 사례 5: 결측 안내용)
MISSING_REASONS = {
    "-": "해당 자료 없음",
    "*": "비밀보호 (5 미만)",
    "x": "비밀보호",
    "X": "비밀보호",
    "…": "미상",
}
# 수집 과정에서 결측 기호가 빠진 표의 기본 결측 사유 (통계표 주석 기준)
TABLE_MISSING_REASON = {"DT_1MR2060": "비밀보호 (개인정보 보호를 위해 5 미만 값 비공개)"}

# ---- 단위 정리 규칙 --------------------------------------------------------
# 원본 unit이 비어 있거나 여러 단위가 한 칸에 합쳐져 있어서 직접 정한다.
UNIT_OVERRIDES = {
    ("DT_1B81A21", "T1"): "가임여성 1명당 명",
    **{("DT_1B81A21", f"T{i}"): "해당 연령 여자인구 1천 명당 명" for i in range(2, 9)},
    ("DT_1B81A23", "T1"): "명",
}
# 지표 이름이 이 정규식에 맞으면 해당 단위 (위에서부터 먼저 맞는 것 적용)
LABEL_UNIT_RULES = [
    ("합계출산율", "가임여성 1명당 명"),
    ("TFR", "가임여성 1명당 명"),
    ("출생성비", "여아 100명당 남아 수"),
    ("(?<!구)성비", "여자 100명당 남자 수"),  # '구성비(%)'는 제외
    ("부양비", "생산연령인구 100명당 명"),
    ("노령화지수", "유소년인구 100명당 명"),
]
# 지표 이름 안 괄호에 단위가 있는 경우. 예: "출생건수 (명)", "고령인구(천명): 65세+"
UNIT_IN_PARENS = re.compile(r"\((명|천명|건|%|천명당|세)\)")
# 원본 unit도 비어 있을 때 쓰는 표 기본 단위
TABLE_DEFAULT_UNIT = {"DT_1BPB101": "여자 1명당 명", "TX_11025_A000_A": "명"}

# ---- 지역 별칭·변경 이력 ---------------------------------------------------
SIDO_ALIASES = {
    "00": ["전국", "전체", "계", "합계"],
    "11": ["서울", "서울시"],
    "26": ["부산", "부산시"],
    "27": ["대구", "대구시"],
    "28": ["인천", "인천시"],
    "29": ["광주"],  # "광주시"는 경기도 광주시와 겹쳐서 제외
    "30": ["대전", "대전시"],
    "31": ["울산", "울산시"],
    "36": ["세종", "세종시"],
    "41": ["경기"],
    "43": ["충북"],
    "44": ["충남"],
    "46": ["전남"],
    "47": ["경북"],
    "48": ["경남"],
    "50": ["제주", "제주도"],
    "51": ["강원", "강원도"],
    "52": ["전북", "전라북도"],
    # 붙여 쓴 이름만 통합특별시로 본다. "광주 전남", "광주, 전남"은 두 지역(29, 46)으로 해석
    "12": ["전남광주", "광주전남"],
}
# 예전 시도 이름 → 현재 이름 (다른 표의 지역 경로를 맞출 때 사용)
OLD_SIDO_NAMES = {"강원도": "강원특별자치도", "전라북도": "전북특별자치도"}
# 전국 합계를 뜻하는 지역 이름
TOTAL_NAMES = {"전국", "계", "합계"}
# 표마다 다르게 쓰는 지역 경로 (공백 제거한 '시도>시군구' 형태)
PATH_FIXES = {"세종특별자치시>세종시": "세종특별자치시>세종특별자치시"}

REGION_CHANGES = [
    (2010, "경남 창원시·마산시·진해시가 창원시로 통합", "48110,48160,48190,48120"),
    (2012, "세종특별자치시 출범 (충남 연기군 폐지)", "36,36110,44730"),
    (2012, "충남 당진군이 당진시로 전환", "44830,44270"),
    (2013, "경기 여주군이 여주시로 전환", "41730,41670"),
    (2014, "충북 청주시·청원군 통합", "43710"),
    (2016, "경기 부천시 일반구(원미·소사·오정) 폐지", "41195,41197,41199"),
    (2018, "인천 남구가 미추홀구로 명칭 변경", "28170,28177"),
    (2023, "경북 군위군이 대구광역시로 편입", "47720,27720"),
    (2023, "강원도가 강원특별자치도로 전환", "51"),
    (2024, "전라북도가 전북특별자치도로 전환", "52"),
    (2024, "경기 부천시 일반구(원미·소사·오정) 재설치", "41192,41194,41196"),
    (2026, "경기 화성시 일반구 신설 (2026-02-01)", "41590"),
    (2026, "광주광역시와 전라남도가 전남광주통합특별시로 통합 (2026-07-01 출범)", "29,46,12"),
    (
        2026,
        "인천 행정구역 개편 (2026-07-01): 중구·동구 → 제물포구·영종구, 서구 → 서해구·검단구",
        "28110,28140,28260",
    ),
]

# 연간(연말 기준) 통계에서 마지막 해. 이후 연도에는 연결하지 않는다.
# 2026년 연간 통계는 연말 기준 행정구역(전남광주통합특별시, 인천 개편 후 구)으로 집계됨
# (국내인구이동통계 주석)
REGION_LAST_YEAR = {
    "29": 2025,
    "46": 2025,  # 광주광역시, 전라남도
    "28110": 2025,
    "28114": 2025,
    "28118": 2025,  # 인천 중구 (영종·용유 출장소 포함)
    "28140": 2025,  # 인천 동구
    "28260": 2025,
    "28265": 2025,  # 인천 서구 (검단 출장소 포함)
}

# 기준 표(2008~2025 주민등록)에는 없지만 이후 출범한 지역.
# 유효 기간이 시작 전이면 자료에 연결하지 않는다.
NEW_REGIONS = [
    # (코드, 이름, 종류, 유효 시작 연도)
    ("12", "전남광주통합특별시", "시도", 2026),
]

# 출범 전 기간이 0으로 채워진 자리값 → 결측으로 처리 (0을 실제 값으로 답하지 않도록)
# 코드: (원본 지역 이름, 출범 연도, 결측 사유)
PLACEHOLDER_REGIONS = {
    "12": (
        "전남광주통합특별시",
        2026,
        "통합특별시 출범(2026-07-01) 전 기간의 자리값으로 실제 자료 아님",
    ),
}


def _norm(text):
    """지역 이름 비교용: 공백 제거 ('중  구' == '중구')"""
    return re.sub(r"\s+", "", text or "")


def resolve_unit(tbl_id, item_id, label, raw_unit):
    """표·항목·지표 이름을 보고 정리된 단위 하나를 정한다."""
    if (tbl_id, item_id) in UNIT_OVERRIDES:
        return UNIT_OVERRIDES[(tbl_id, item_id)]
    for pattern, unit in LABEL_UNIT_RULES:
        if re.search(pattern, label):
            return unit
    m = UNIT_IN_PARENS.search(label)
    if m:
        return m.group(1)
    if raw_unit:
        return raw_unit
    return TABLE_DEFAULT_UNIT.get(tbl_id)


def build_catalog(con):
    """1. *.meta.json → catalog 테이블"""
    rows = []
    for path in sorted(RAW_DIR.glob("*.meta.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        tbl_id = meta["tbl_id"]
        rows.append(
            {
                "tbl_id": tbl_id,
                "tbl_nm": meta["tbl_nm"],
                "org_id": meta["org_id"],
                "org_nm": ORG_NAMES.get(meta["org_id"]),
                "start_year": int(meta["start"]),
                "end_year": int(meta["end"]),
                "is_projection_table": tbl_id in PROJECTION_TABLES,
                "age_unit": AGE_UNIT.get(tbl_id),
                "items": json.dumps(meta["items"], ensure_ascii=False),
                "classes": json.dumps(meta["classes"], ensure_ascii=False),
                "rows": meta["rows"],
            }
        )
    con.register("catalog_df", pd.DataFrame(rows))
    con.sql("create table catalog as select * from catalog_df")
    con.unregister("catalog_df")


def load_raw(con):
    """CSV 20개를 한 번에 읽어 raw 테이블로. 코드값 앞자리 0을 지키려고 전부 문자열로 읽는다."""
    files = [p.as_posix() for p in sorted(RAW_DIR.glob("*.csv"))]
    con.sql(f"""
        create temp table raw as
        select * from read_csv({files!r}, union_by_name = true, all_varchar = true)
    """)
    label_sql = " ".join(f"when '{t}' then {col}" for t, col in LABEL_COLUMN.items())
    con.sql("alter table raw add column label varchar")
    con.sql(f"update raw set label = case tbl_id {label_sql} else item_nm end")


def build_regions(con):
    """3. 1세별 주민등록인구 표의 지역 코드로 regions 테이블 생성

    kind 구분
      전국 / 시도
      시군구 : 기초자치단체. 시도 합계 = 시군구 합계 (2008~2025 전 연도 확인)
      일반구 : 자치구가 아닌 시 아래 구 (예: 수원시 장안구). 합산하면 중복
      출장소 : 행정 출장소. 합산하면 중복
    """
    con.sql("""
        create temp table r0 as
        select c1_id as region_code, any_value(c1_nm) as region_name,
               any_value(c1_level)::int as lvl,
               min(period)::int as first_year, max(period)::int as last_year
        from raw where tbl_id = 'DT_1B04006'
        group by c1_id
    """)
    con.sql("""
        create table regions as
        select r.region_code, r.region_name,
               case when r.region_code = '00' then '전국'
                    when r.lvl = 1 then '시도'
                    when r.region_name like '%출장소' then '출장소'
                    when exists (
                        select 1 from r0 c
                        where c.lvl = 2 and c.region_code <> r.region_code
                          and c.region_code like '%0' and c.region_name like '%시%'
                          and left(c.region_code, 4) = left(r.region_code, 4)
                    ) then '일반구'
                    else '시군구' end as kind,
               case when r.lvl = 1 then null else left(r.region_code, 2) end as sido_code,
               r.first_year, r.last_year
        from r0 r
    """)
    con.sql("""
        alter table regions add column sido_name varchar;
        update regions set sido_name = s.region_name
        from regions s where s.region_code = regions.sido_code;
        alter table regions add column is_basic boolean;
        update regions set is_basic = (kind = '시군구');
        alter table regions add column path varchar;
        update regions set path = case when sido_name is null then region_name
                                       else sido_name || ' > ' || region_name end;
    """)
    # 유효 기간: 기준 표 수록 기간(2008~2025)의 처음/끝까지 있던 지역은
    # 그 이전/이후도 유효(NULL)로 본다.
    # 예) 서울특별시 valid_from=NULL → 1970년 자료도 연결, 군위군(대구) valid_from=2023
    con.sql("""
        alter table regions add column valid_from int;
        alter table regions add column valid_to int;
        update regions set
            valid_from = case when first_year = (select min(first_year) from regions)
                              then null else first_year end,
            valid_to   = case when last_year  = (select max(last_year)  from regions)
                              then null else last_year  end;
    """)
    for code, last in REGION_LAST_YEAR.items():
        con.execute("update regions set valid_to = ? where region_code = ?", [last, code])
    # 기준 표 이후 출범한 지역 추가 (자료가 없으니 first_year/last_year는 비워 둔다)
    for code, name, kind, valid_from in NEW_REGIONS:
        con.execute(
            """
            insert into regions (region_code, region_name, kind, is_basic, path, valid_from)
            values (?, ?, ?, false, ?, ?)
        """,
            [code, name, kind, name, valid_from],
        )

    alias_rows = [(a, code) for code, names in SIDO_ALIASES.items() for a in names]
    alias_rows += con.sql("select region_name, region_code from regions").fetchall()
    con.register("alias_df", pd.DataFrame(alias_rows, columns=["alias", "region_code"]))
    con.sql("create table region_aliases as select distinct alias, region_code from alias_df")
    con.unregister("alias_df")

    con.register(
        "chg_df", pd.DataFrame(REGION_CHANGES, columns=["year", "description", "region_codes"])
    )
    con.sql("create table region_changes as select * from chg_df")
    con.unregister("chg_df")


def build_region_map(con):
    """표마다 다른 지역 코드를 regions.region_code로 맞추는 대응표 (region_code_map)

    통계청 표 중에도 예전 코드 체계를 쓰는 표가 있다 (예전 31=경기도, 현재 31=울산광역시).
    그래서 코드만 보고 연결하지 않고, 아래 순서로 이름까지 확인한다. (match_method)
      total : 전국/계/합계
      code  : 코드가 같고 이름도 같음
      path  : '시도 > 시군구' 이름 경로가 같음 (3단계 경로는 시도와 마지막 이름만 사용)
      sido  : 시도 이름이 같음 (예전 시도 이름 포함)
    같은 경로에 코드가 둘 이상이면(예: 부천시 원미구 41195 → 41192) 후보를 모두 남기고
    facts에서 연도로 하나를 고른다. 연결하지 못하면 unmatched_reason을 남긴다.
    """
    parts = []
    for (tbl_id,) in con.sql("select distinct tbl_id from raw").fetchall():
        col = REGION_COLUMN.get(tbl_id, "c1")
        if col is None:
            continue
        parts.append(f"""
            select distinct tbl_id, org_id, {col}_id as src_code, {col}_nm as src_name,
                   {col}_level as src_level, {col}_path as src_path
            from raw where tbl_id = '{tbl_id}'
        """)
    src = con.sql(" union all ".join(parts)).df()

    regions = con.sql("select region_code, region_name, kind, path from regions").df()
    name_by_code = {
        c: _norm(n) for c, n in zip(regions.region_code, regions.region_name, strict=True)
    }
    codes_by_path = {}
    for code, path in zip(regions.region_code, regions.path, strict=True):
        codes_by_path.setdefault(_norm(path), []).append(code)
    sido_by_name = {
        _norm(n): c
        for c, n, k in zip(regions.region_code, regions.region_name, regions.kind, strict=True)
        if k == "시도"
    }

    def candidates(row):
        name = _norm(row.src_name)
        if name in TOTAL_NAMES and str(row.src_level) == "1":
            return ["00"], "total"
        if row.org_id == "101" and name_by_code.get(row.src_code) == name:
            return [row.src_code], "code"
        parts = [
            _norm(OLD_SIDO_NAMES.get(p.strip(), p.strip())) for p in (row.src_path or "").split(">")
        ]
        parts = [p for p in parts if p and p not in TOTAL_NAMES]
        if len(parts) >= 2:
            key = PATH_FIXES.get(f"{parts[0]}>{parts[-1]}", f"{parts[0]}>{parts[-1]}")
            if key in codes_by_path:
                return codes_by_path[key], "path"
        if len(parts) == 1 and parts[0] in sido_by_name:
            return [sido_by_name[parts[0]]], "sido"
        return [], None

    def unmatched_reason(name):
        name = name or ""
        if "변동전" in name:
            return "변동전 지역 (현재 지역과 범위가 달라 연결하지 않음)"
        if _norm(name) in {"동부", "읍부", "면부"}:
            return "지역이 아닌 동·읍·면부 분류"
        if "국외" in name:
            return "국외"
        return "기준 지역 목록(2008~2025 주민등록)에 없는 지역"

    rows = []
    for row in src.itertuples():
        codes, method = candidates(row)
        base = {
            "tbl_id": row.tbl_id,
            "src_code": row.src_code,
            "src_name": row.src_name,
            "src_path": row.src_path,
        }
        if codes:
            rows += [
                {**base, "region_code": c, "match_method": method, "unmatched_reason": None}
                for c in codes
            ]
        else:
            rows.append(
                {
                    **base,
                    "region_code": None,
                    "match_method": None,
                    "unmatched_reason": unmatched_reason(row.src_name),
                }
            )

    con.register("map_df", pd.DataFrame(rows))
    con.sql("""
        create table region_code_map as
        select m.*, g.valid_from, g.valid_to
        from map_df m left join regions g using (region_code)
    """)
    con.unregister("map_df")


def build_facts(con):
    """2. 모든 표를 하나의 세로형 facts 테이블로 (지역 코드·단위·결측·추계 정리)"""
    keys = con.sql("select distinct tbl_id, item_id, label, unit from raw").df()
    keys["clean_unit"] = [
        resolve_unit(
            r.tbl_id, r.item_id, r.label or "", r.unit if isinstance(r.unit, str) else None
        )
        for r in keys.itertuples()
    ]
    con.register("unit_df", keys)

    region_sql = " ".join(
        f"when '{t}' then {col}_id" if col else f"when '{t}' then null"
        for t, col in REGION_COLUMN.items()
    )
    region_nm_sql = " ".join(
        f"when '{t}' then {col}_nm" if col else f"when '{t}' then null"
        for t, col in REGION_COLUMN.items()
    )
    src_code = f"case r.tbl_id {region_sql} else r.c1_id end"
    src_name = f"case r.tbl_id {region_nm_sql} else r.c1_nm end"
    # 출범 전 자리값이면 사유 문자열, 아니면 NULL
    placeholder_reason = (
        "case "
        + " ".join(
            f"when {src_code} = '{code}' and {src_name} = '{name}' "
            f"and r.period::int < {year} then '{reason}'"
            for code, (name, year, reason) in PLACEHOLDER_REGIONS.items()
        )
        + " end"
    )

    missing_sql = " ".join(f"when '{k}' then '{v}'" for k, v in MISSING_REASONS.items())
    table_missing_sql = " ".join(f"when '{t}' then '{v}'" for t, v in TABLE_MISSING_REASON.items())
    proj_list = ", ".join(f"'{t}'" for t in PROJECTION_TABLES)

    con.sql(f"""
        create table facts as
        select r.tbl_id,
               r.period::int as year,
               r.item_id, r.item_nm, r.label,
               m.region_code, m.match_method as region_match,
               r.c1_id, r.c1_nm, r.c2_id, r.c2_nm, r.c3_id, r.c3_nm,
               case when {placeholder_reason} is null then try_cast(r.value as double) end as value,
               u.clean_unit as unit,
               r.value is null or {placeholder_reason} is not null as is_missing,
               r.value_flag as missing_symbol,
               coalesce({placeholder_reason},
                        case when r.value is null
                             then coalesce(case r.value_flag {missing_sql} end,
                                           case r.tbl_id {table_missing_sql} end,
                                           '값 없음') end) as missing_reason,
               case when r.tbl_id in ({proj_list})
                    then trim(split_part(r.c1_nm, '(', 1)) end as scenario,
               r.tbl_id in ({proj_list})
                   and r.period::int > {PROJECTION_BASE_YEAR} as is_projection,
               try_cast(r.last_changed as date) as last_changed
        from raw r
        left join unit_df u
               on u.tbl_id = r.tbl_id and u.item_id = r.item_id
              and u.label is not distinct from r.label and u.unit is not distinct from r.unit
        left join region_code_map m
               on m.tbl_id = r.tbl_id
              and m.src_code = {src_code}
              -- 그 연도에 유효한 코드만 (예: 부천 원미구 2015년 → 41195, 2024년 → 41192)
              -- 장래추계는 추계 당시(2022년) 행정구역 기준이라 예외 (예: 광주광역시 2030년 추계)
              and (r.tbl_id in ({proj_list}) or (
                   (m.valid_from is null or r.period::int >= m.valid_from)
                   and (m.valid_to is null or r.period::int <= m.valid_to)))
    """)
    con.unregister("unit_df")


def build_population(con):
    """4. 1세별 주민등록인구 → population (age가 NULL이면 전체 연령 합계)"""
    con.sql("""
        create table population as
        select period::int as year,
               c1_id as region_code,
               case item_id when 'T2' then '계' when 'T3' then '남' when 'T4' then '여' end as sex,
               case when c2_nm = '계' then null
                    else regexp_extract(c2_nm, '(\\d+)', 1)::int end as age,
               c2_nm like '%이상' as is_open_age,
               c2_nm as age_label,
               value::bigint as population
        from raw where tbl_id = 'DT_1B04006'
    """)


def print_summary(con):
    print("\n[테이블 행 수]")
    for t in [
        "catalog",
        "regions",
        "region_aliases",
        "region_changes",
        "region_code_map",
        "facts",
        "population",
    ]:
        print(f"  {t:16s} {con.sql(f'select count(*) from {t}').fetchall()[0][0]:>10,}")

    print("\n[지역 종류]")
    print(
        con.sql("select kind, count(*) n from regions group by 1 order by 1")
        .df()
        .to_string(index=False)
    )

    print("\n[지역 연결 방식] (원본 지역 코드 기준)")
    print(
        con.sql("""
        select coalesce(match_method, '연결 안 함') as method, count(*) as n
        from region_code_map group by 1 order by 2 desc
    """)
        .df()
        .to_string(index=False)
    )

    print("\n[연결하지 않은 지역 사유]")
    print(
        con.sql("""
        select unmatched_reason, count(*) as n, string_agg(distinct tbl_id, ', ') as tables
        from region_code_map where region_code is null group by 1 order by 2 desc
    """)
        .df()
        .to_string(index=False)
    )

    print("\n[지역 연결 검증] 모두 0이어야 정상")
    checks = {
        "코드는 연결됐지만 이름이 다른 행": """
            select count(*) from region_code_map m join regions r using (region_code)
            where m.match_method = 'code'
              and regexp_replace(m.src_name, '\\s', '', 'g')
                  <> regexp_replace(r.region_name, '\\s', '', 'g')""",
        "연결 과정에서 늘어난/줄어든 행 수": (
            "select (select count(*) from facts) - (select count(*) from raw)"
        ),
        "같은 표·연도·지역에 원본 코드가 2개 이상 (합산 시 중복)": """
            select count(*) from (
                select tbl_id, year, region_code, c1_id, c2_id, c3_id, item_id
                from facts where region_code is not null
                group by all having count(*) > 1)""",
        "유효 기간 밖 연도에 연결된 행 (장래추계 제외)": f"""
            select count(*) from facts f join regions r using (region_code)
            where f.tbl_id not in ({", ".join(f"'{t}'" for t in PROJECTION_TABLES)})
              and ((r.valid_from is not null and f.year < r.valid_from)
                   or (r.valid_to is not null and f.year > r.valid_to))""",
    }
    for title, sql in checks.items():
        print(f"  {title}: {con.sql(sql).fetchall()[0][0]}")

    print("\n[단위가 정해지지 않은 지표]")
    print(
        con.sql("""
        select distinct tbl_id, item_id, label from facts where unit is null order by 1, 2
    """)
        .df()
        .to_string(index=False)
        or "  없음"
    )

    print("\n[결측] (표별)")
    print(
        con.sql("""
        select tbl_id, count(*) filter (where is_missing) as missing,
               string_agg(distinct missing_reason, ', ') as reasons
        from facts group by 1 having missing > 0 order by 1
    """)
        .df()
        .to_string(index=False)
    )

    bad = con.sql("select count(*) from facts where not is_missing and value is null").fetchall()[
        0
    ][0]
    print(f"\n[숫자 변환 실패] {bad}건")


def main():
    if not RAW_DIR.exists():
        raise SystemExit(f"원본 폴더가 없습니다: {RAW_DIR}")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.unlink(missing_ok=True)

    con = duckdb.connect(str(OUT_PATH))
    print("1/5 catalog")
    build_catalog(con)
    print("2/5 raw 읽기")
    load_raw(con)
    print("3/5 regions")
    build_regions(con)
    build_region_map(con)
    print("4/5 facts")
    build_facts(con)
    print("5/5 population")
    build_population(con)
    print_summary(con)
    con.close()
    print(f"\n완료: {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
