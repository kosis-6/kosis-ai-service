"""통계표 패밀리 묶기: stat_tables → table_families, family_axes

실전 2 기능 정의서 F-19 ③④ (검색 규칙 1장). 표를 합치지 않고 패밀리 → 멤버 → 판 관계만 기록한다.
검색은 표 단위로 하고, 결과를 패밀리로 접은 뒤 멤버·판을 고를 때(F-18) 이 테이블을 쓴다.

묶는 규칙
  판      같은 조사 + 이름 속 연도·신구 표기를 지운 표명이 같은 표.
          '시도별 외국인주민 현황(2007~2015)' / '(2006)' / 연도 없는 표 → 판 3개.
          띄어쓰기·기호만 다른 표명도 같은 표로 본다 (name_key, 팀원 검색 실험 10/31).
          '성별 외국인 - 시군구' / '성별 외국인-시군구', '통근ㆍ통학' / '통근 통학' (52묶음).
          '및'·'의'·'별'만 다른 표명도 같은 표다 (규칙 1.1 ②, scripts/family_check.py R1).
          '성, 연령 및 직업별 인구'(2010) / '성별/연령별/직업별 인구'(2020)
          단, 이름이 같아도 수록 기간이 똑같고 분류 구성이 다른 표는 판이 아니라 다른 표로 나눈다
          ('시도/산업,직업/성별 취업자' / '시도/산업/직업/성별 취업자', 둘 다 1975)
          보여 줄 이름은 최신 판의 표기를 쓴다
          수록 기간으로 구분하고, edition_order 1이 기본 판이다
          (가장 최신 연도를 담은 판 → 그중 기간이 긴 판, 규칙 2.4)
  멤버    표명 끝의 (계)/(남)/(여)로 성별 멤버, 그 앞의 '-대상'으로 대상 멤버를 나눈다.
          '한국국적을 취득한 자-혼인 귀화자(남)' → 패밀리 '한국국적을 취득한 자',
          대상 '혼인 귀화자', 성별 '남'. '-대상'이 없으면 대상 '전체'.
  대상 분할  성별 표기가 없어도 같은 조사 + 같은 분류명 구성 + '-' 앞부분이 같은 표가 둘 이상이면
          '-' 뒤를 대상으로 본다 (규칙 1.2 ②, scripts/family_check.py R2).
          '차별 받은 경험 - 병원 및 복지기관' / '- 가게 및 식당' → 패밀리 '차별 받은 경험'
          '-' 뒤가 지역 단위(시도·시군구·읍면동)면 대상이 아니다
  지역 분할  같은 조사 + 같은 폴더 + 같은 수록 기간 + 같은 분류명·항목명 구성이고 표명이 시도 이름만
          다르면 지역 멤버로 본다 (규칙 1.2 ①, family_check.py R3). 지역 이름은 target에 넣는다.
          '통근통학 인구(서울, 인천, 경기)-시군구' / '(강원)-시군구'
            → 대상 '서울·인천·경기' / '강원'
          같은 폴더·구조라도 이름이 시도 이름 말고 다르면('혼인상태별' / '교육정도별') 묶지 않는다
  '-시도'·'-시군구'처럼 지역 수준만 다른 표는 다른 패밀리로 둔다 (stat_tables.region_levels로 판정)
  후속판  이름이 바뀌어 이어지는 표. 표 ID가 옛 표 ID + '_A'이고 같은 조사, 분류 ID 구성이
          같으면 옛 표의 멤버에 판으로 붙인다. 대상 이름은 최신 표 기준.
          'TX_11025_A017 한국국적을 취득한 자(계)(2006~2015)'
            → 'TX_11025_A017_A 이전국적별 한국국적을 취득한 자(계)(2015~2024)'
          'TX_11025_A008 …-국제결혼 이주자(계)' → 'TX_11025_A008_A …-결혼이민자(계)'
          ID는 맞는데 분류 구성이 다른 표는 붙이지 않고 보고에 '확인 필요'로 남긴다

출력
  table_families  표마다 family_id, family_name, target, sex, edition_order, edition,
                  renamed_from(후속판이면 이름이 바뀌기 전 표 ID)
  family_axes     패밀리를 가르는 축(target / sex / edition)과 선택지. 선택지가 2개 이상인 축만
                  (F-14가 질문에 없는 축만 골라 되묻는다)
  두 테이블 모두 stat_tables에서 다시 만들 수 있으므로 실행할 때마다 새로 만든다.

실행 (stat_tables 적재 후, 서버 컨테이너 안)
  docker compose exec api python -m pipeline.build_families            # 묶기 + 보고
  docker compose exec api python -m pipeline.build_families --dry-run  # DB 없이 카탈로그로 보고만
"""

import argparse
import re
import sys
from collections import Counter, defaultdict
from typing import Any

# 이름 속 판 표기: '(2007~2015)', '(2006)', '(2011년~)', '(~1992)', '(2023~)'
EDITION_YEAR = re.compile(r"\s*\(\s*~?\s*\d{4}\s*년?\s*(?:~\s*(?:\d{4}\s*년?)?)?\s*\)")
# 분류 개정 판: '직업별(신) 사망자수(1993~2017)' / '직업별(구) 사망자수(~1992)'
EDITION_REVISION = re.compile(r"\((?:신|구)\)")
SEX_MEMBER = re.compile(r"\((계|남|여)\)")
SUCCESSOR_ID = re.compile(r"(.+)_A")
TOTAL_TARGET = "전체"
# '-' 뒤가 이것뿐이면 대상이 아니라 지역 수준 ('외국인-시군구')
REGION_LEVEL_SUFFIX = re.compile(r"^\s*(시도|시군구|읍면동)\s*$")
# 표명 속 시도 이름. 긴 이름을 먼저 맞춘다 ('경기도'가 '경기'보다 먼저)
SIDO = re.compile(
    "서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|세종특별자치시|"
    "경기도|강원특별자치도|강원도|충청북도|충청남도|전북특별자치도|전라북도|전라남도|경상북도|"
    "경상남도|제주특별자치도|제주도|서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|"
    "전북|전남|경북|경남|제주"
)

DDL = """
DROP TABLE IF EXISTS table_families CASCADE;
CREATE TABLE table_families (
    tbl_id         text PRIMARY KEY REFERENCES stat_tables (tbl_id) ON DELETE CASCADE,
    family_id      text NOT NULL,
    family_name    text NOT NULL,
    target         text,
    sex            text,
    edition_order  int NOT NULL,
    edition        text NOT NULL,
    renamed_from   text
);
CREATE INDEX table_families_family_idx ON table_families (family_id);
CREATE VIEW family_axes AS
WITH options AS (
    SELECT family_id, 'target' AS axis, target AS option, min(edition_order) AS sort_key
    FROM table_families WHERE target IS NOT NULL GROUP BY family_id, target
    UNION ALL
    SELECT family_id, 'sex', sex, min(edition_order)
    FROM table_families WHERE sex IS NOT NULL GROUP BY family_id, sex
    UNION ALL
    -- 판은 최신 연도를 담은 판 → 기간이 긴 판 순
    SELECT f.family_id, 'edition', f.edition, min(s.start_year - s.end_year * 10000)
    FROM table_families f JOIN stat_tables s USING (tbl_id) GROUP BY f.family_id, f.edition
)
SELECT family_id, axis, array_agg(option ORDER BY option <> '전체', sort_key, option) AS options
FROM options
GROUP BY family_id, axis
HAVING count(*) > 1;
"""


def edition_name(table_name: str) -> str:
    """판 표기를 지운 표명 ('…(계)(2007~2015)' → '…(계)')"""
    name = EDITION_REVISION.sub("", EDITION_YEAR.sub("", table_name))
    return re.sub(r"\s+", " ", name).strip()


def name_key(name: str) -> str:
    """띄어쓰기·기호와 '및'·'의'·'별'을 지운 비교용 표명 (규칙 1.1 ②).
    '성별 외국인 - 시군구' = '성별 외국인-시군구'
    '성, 연령 및 직업별 인구' = '성별/연령별/직업별 인구'
    """
    return re.sub(r"[^0-9A-Za-z가-힣]|및|의|별", "", name)


def member_of(name: str) -> tuple[str, str | None, str | None]:
    """(family_name, target, sex). 이름에 성별 표기가 없으면 멤버가 하나뿐인 패밀리"""
    sex = SEX_MEMBER.search(name)
    if not sex:
        return name, None, None
    base = SEX_MEMBER.sub("", name).strip()
    family, _, target = base.rpartition("-")
    if not family:
        return base, TOTAL_TARGET, sex.group(1)
    return family.strip(), target.strip(), sex.group(1)


def without_sex(name: str) -> str:
    return re.sub(r"\s+", " ", SEX_MEMBER.sub("", name)).strip()


def strip_region(name: str) -> str:
    """표명에서 시도 이름을 지운다.

    '통근통학 인구(서울, 인천, 경기)(12세 이상)' → '통근통학 인구(12세 이상)'
    빈 괄호와 괄호 안에 남은 쉼표·'-'도 지운다 ('(대구,경북-시군구)' → '(시군구)')
    """
    n = SIDO.sub("", name)
    n = re.sub(r"\(\s*[,\s-]*", "(", n)
    n = re.sub(r"[,\s]*\)", ")", n)
    n = re.sub(r"\(\s*\)", "", n)
    return re.sub(r"\s+", " ", n).strip()


def region_label(name: str) -> str:
    """'(서울, 인천, 경기)' → '서울·인천·경기'"""
    return "·".join(dict.fromkeys(m.group(0) for m in SIDO.finditer(name)))


def _names(items: list[dict[str, Any]] | None) -> tuple[str, ...]:
    return tuple(sorted(i["name"] for i in items or []))


def split_members(
    tables: list[dict[str, Any]], member_name: Any
) -> dict[tuple[str, str], tuple[str, str]]:
    """이름에 (계)/(남)/(여)가 없는 대상·지역 멤버 → {(조사, name_key(멤버명)): (패밀리명, 대상)}

    member_name(t): 표의 멤버 이름 (후속판은 옛 표 이름, 판 표기 제거)
    """
    out: dict[tuple[str, str], tuple[str, str]] = {}

    # 대상 분할: 같은 조사 + 같은 분류명 구성 + '-' 앞부분
    by_prefix: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for t in tables:
        name = without_sex(member_name(t))
        prefix, sep, suffix = name.rpartition("-")
        if sep and prefix.strip() and not REGION_LEVEL_SUFFIX.match(suffix):
            key = (t["survey_name"], name_key(prefix), _names(t.get("classifications")))
            by_prefix[key].add(name)
    for (survey, _, _), names in by_prefix.items():
        if len({name_key(n) for n in names}) < 2:
            continue
        for n in names:
            prefix, _, suffix = n.rpartition("-")
            out[(survey, name_key(n))] = (prefix.strip(), suffix.strip())

    # 지역 분할: 같은 조사·폴더·기간·분류명·항목명, 표명은 시도 이름만 다름
    by_shape: dict[tuple[Any, ...], set[str]] = defaultdict(set)
    for t in tables:
        folder = ((t.get("path_texts") or [""])[0]).rsplit(" > ", 1)[0]
        key = (t["survey_name"], folder, t["start_year"], t["end_year"],
               _names(t.get("classifications")), _names(t.get("items")))  # fmt: skip
        by_shape[key].add(without_sex(member_name(t)))
    for (survey, *_), names in by_shape.items():
        if len({name_key(n) for n in names}) < 2 or not all(SIDO.search(n) for n in names):
            continue
        if len({name_key(strip_region(n)) for n in names}) != 1:
            continue
        for n in names:
            out.setdefault((survey, name_key(n)), (strip_region(n), region_label(n)))
    return out


def separate_same_period(
    grouped: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[tuple[str, str, int], list[dict[str, Any]]]:
    """같은 멤버로 모인 표 중 수록 기간이 똑같은 표가 있고 분류 구성이 다르면 판이 아니라 다른 표다.
    분류 구성별로 나눠 다른 패밀리로 둔다 (scripts/edition_check.py B: 기호만 다른 이름).
    예 (둘 다 1975):
      '시도/산업(대분류),직업(대분류)/성별 취업자'  분류: 시도·산업직업
      '시도/산업(대분류)/직업(대분류)/성별 취업자'  분류: 시도·성별·직업
    """
    out: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for (survey, key), ts in grouped.items():
        periods = Counter((t["start_year"], t["end_year"]) for t in ts)
        shapes: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for t in ts:
            shapes[_names(t.get("classifications"))].append(t)
        if max(periods.values()) == 1 or len(shapes) == 1:
            out[(survey, key, 0)] = ts
            continue
        for i, shape in enumerate(sorted(shapes)):
            out[(survey, key, i)] = shapes[shape]
    return out


def edition_label(start_year: int, end_year: int) -> str:
    return str(start_year) if start_year == end_year else f"{start_year}~{end_year}"


def class_ids(t: dict[str, Any]) -> list[str]:
    return sorted(c.get("id") or c["name"] for c in t.get("classifications") or [])


def successors(tables: list[dict[str, Any]]) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """(새 표 ID → 옛 표 ID, 분류 구성이 달라 붙이지 않은 쌍)"""
    by_id = {t["tbl_id"]: t for t in tables}
    linked, unsure = {}, []
    for t in tables:
        m = SUCCESSOR_ID.fullmatch(t["tbl_id"])
        old = by_id.get(m.group(1)) if m else None
        if old is None or old["survey_name"] != t["survey_name"]:
            continue
        if class_ids(old) == class_ids(t):
            linked[t["tbl_id"]] = old["tbl_id"]
        else:
            unsure.append((old["tbl_id"], t["tbl_id"]))
    return linked, unsure


def build(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """tables: tbl_id, survey_name, table_name, start_year, end_year, classifications
    (지역 분할 판정에는 items, path_texts도)를 가진 목록"""
    linked, _ = successors(tables)
    by_id = {t["tbl_id"]: t for t in tables}
    # 이름까지 바뀐 경우만 기록 (ID에 '_A'만 붙고 표명이 같은 판은 원래 같은 멤버)
    renamed = {
        new: old
        for new, old in linked.items()
        if edition_name(by_id[new]["table_name"]) != edition_name(by_id[old]["table_name"])
    }

    def anchor_name(t: dict[str, Any]) -> str:
        """후속판은 옛 표의 이름으로 (옛 표의 멤버에 붙인다)"""
        return edition_name(by_id[linked.get(t["tbl_id"], t["tbl_id"])]["table_name"])

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for t in tables:
        grouped[(t["survey_name"], name_key(anchor_name(t)))].append(t)
    members = separate_same_period(grouped)
    splits = split_members(tables, anchor_name)

    rows = []
    for (survey, _, variant), editions in members.items():
        # 최신 연도를 담은 판 → 기간이 긴 판 → 시작 연도가 이른 판 (규칙 2.4)
        editions.sort(key=lambda t: (-t["end_year"], t["start_year"], t["tbl_id"]))
        name = anchor_name(editions[0])
        family_name, _, sex = member_of(name)
        # 대상 이름은 최신 판 표명 기준 ('국제결혼 이주자' → '결혼이민자')
        target = member_of(edition_name(editions[0]["table_name"]))[1] or member_of(name)[1]
        split = splits.get((survey, name_key(without_sex(name))))
        if target is None and split:
            family_name, target = split
        for order, t in enumerate(editions, start=1):
            rows.append(
                {
                    "tbl_id": t["tbl_id"],
                    "survey_name": survey,
                    "family_name": family_name,
                    "target": target,
                    "sex": sex,
                    "edition_order": order,
                    "edition": edition_label(t["start_year"], t["end_year"]),
                    "renamed_from": renamed.get(t["tbl_id"]),
                    "_variant": variant,
                }
            )

    # family_id는 조사·패밀리명 순서로 매긴다 (카탈로그가 같으면 같은 번호).
    # 멤버끼리 패밀리명 표기가 다르면 ('취득한 자' / '취득한자') 가장 많이 쓰인 표기로
    def family_key(r: dict[str, Any]) -> tuple[str, str, int]:
        return r["survey_name"], name_key(r["family_name"]), r["_variant"]

    spellings: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for r in rows:
        spellings[family_key(r)][r["family_name"]] += 1
    keys = sorted(spellings, key=lambda k: (k[0], spellings[k].most_common(1)[0][0], k[2]))
    ids = {k: f"FAM_{i:04d}" for i, k in enumerate(keys, start=1)}
    for r in rows:
        k = family_key(r)
        r["family_id"], r["family_name"] = ids[k], spellings[k].most_common(1)[0][0]
    for r in rows:
        del r["_variant"]
    # 대상으로 나뉜 패밀리에 '-대상' 없는 표가 함께 묶이면 그 표가 '전체'
    split_families = {r["family_id"] for r in rows if r["target"] is not None}
    for r in rows:
        if r["target"] is None and r["family_id"] in split_families:
            r["target"] = TOTAL_TARGET
    return sorted(
        rows, key=lambda r: (r["family_id"], r["target"] or "", r["sex"] or "", r["edition_order"])
    )


def report(rows: list[dict[str, Any]], unsure: list[tuple[str, str]] | None = None) -> str:
    size = Counter(r["family_id"] for r in rows)
    multi = {f for f, n in size.items() if n > 1}
    member_families = {
        r["family_id"] for r in rows if r["sex"] or r["target"] not in (None, TOTAL_TARGET)
    }
    edition_groups = Counter((r["family_id"], r["target"], r["sex"]) for r in rows)
    names = {r["family_id"]: r["family_name"] for r in rows}
    lines = [
        f"표 {len(rows)}개 → 패밀리 {len(size)}개 (표 2개 이상 {len(multi)}개,"
        f" {sum(size[f] for f in multi)}개 표)",
        f"판이 2개 이상인 멤버: {sum(n > 1 for n in edition_groups.values())}개"
        f" ({sum(n for n in edition_groups.values() if n > 1)}개 표)",
        f"대상·성별·지역 멤버로 나뉜 패밀리: {len(member_families)}개"
        f" ({sum(size[f] for f in member_families)}개 표)",
        "표가 많은 패밀리: " + ", ".join(f"{names[f]} {n}" for f, n in size.most_common(5)),
        f"이름이 바뀐 후속판 연결: {sum(r['renamed_from'] is not None for r in rows)}개 표",
    ]
    if unsure:
        lines.append(f"후속판 확인 필요 (ID는 맞는데 분류 구성이 다름): {unsure}")
    return "\n".join(lines)


def load(rows: list[dict[str, Any]], url: str) -> int:
    import psycopg

    cols = [
        "tbl_id",
        "family_id",
        "family_name",
        "target",
        "sex",
        "edition_order",
        "edition",
        "renamed_from",
    ]
    with psycopg.connect(url) as conn:  # 블록이 끝나면 커밋, 오류면 전부 되돌림
        conn.execute(DDL)
        with conn.cursor().copy(f"COPY table_families ({', '.join(cols)}) FROM STDIN") as copy:
            for r in rows:
                copy.write_row(tuple(r[c] for c in cols))
        return conn.execute("SELECT count(*) FROM family_axes").fetchone()[0]


def read_tables(url: str) -> list[dict[str, Any]]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        return conn.execute(
            "SELECT tbl_id, survey_name, table_name, start_year, end_year, classifications,"
            " items, path_texts"
            " FROM stat_tables"
        ).fetchall()


def main() -> int:
    from pipeline.load_catalog import database_url, read_catalog

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="DB 없이 카탈로그로 묶기·보고만")
    args = parser.parse_args()

    if args.dry_run:
        tables = read_catalog()
        rows = build(tables)
        print(report(rows, successors(tables)[1]))
        print("\n--dry-run: DB에는 넣지 않음")
        return 0
    url = database_url()
    tables = read_tables(url)
    if not tables:
        print("stat_tables가 비어 있음. pipeline.load_catalog를 먼저 실행", file=sys.stderr)
        return 1
    rows = build(tables)
    print(report(rows, successors(tables)[1]))
    axes = load(rows, url)
    print(f"\ntable_families 적재 완료: {len(rows)}행 · family_axes {axes}행")
    return 0


if __name__ == "__main__":
    sys.exit(main())
