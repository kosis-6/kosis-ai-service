"""표 하나의 지역 대응표(mart.region_code_map)를 만든다 — 적재 후 단계 (AFTER_LOAD)

표마다 지역 코드를 다르게 쓴다 (예전 코드 체계, 이름 경로만 같은 표 등). 표의 원래 지역 코드를
기준 지역(mart.regions, 2008~2025 주민등록 기준) 코드로 잇는 줄을 region_code_map에 넣는다.
서비스(app/services/lookup.py)는 이 대응이 있어야 그 표에서 지역 조건으로 값을 찾는다.

  preprocess.py   서비스 기본 표 20개: CSV에서 지역 목록을 읽어 build 스키마에 한 번에 만든다
  이 파일         그 밖에 적재한 표: catalog.stat_code(적재 때 저장한 분류 코드)에서 지역 목록을
                  읽어 표 하나씩 만든다. 연결 규칙(match_region)은 두 곳이 같이 쓴다

지역 칸 찾기 (find_region_dim)
  REGION_COLUMN에 적은 표는 그대로 따르고, 나머지는 분류 이름에 REGION_WORDS가 들어간 첫 칸.
  (행정구역별, 시도별, 행정구역별(시군구), 현거주지별, 전출지별 …. 국적(지역)별은 국외라 제외)
  지역처럼 보여도 기준 지역과 하나도 이어지지 않으면 그 표는 대응을 만들지 않는다.

실행 (backend/ 에서)
  python -m pipeline.region_map                 적재됐는데 대응이 없는 표 전부
  python -m pipeline.region_map DT_1B26001 …    지정한 표 (있으면 다시 만든다)
  ※ run_all.py는 publish_stats.py가 region_code_map을 기본 표 것으로 바꾼 뒤 이 파일을 다시 실행한다
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("region_map")

# 지역 분류가 이름으로 찾은 칸이 아닌 표 (None = 지역 분류 없는 전국 자료)
REGION_COLUMN = {
    "DT_1BPB002": "c2",
    "DT_1BPB003": "c2",
    "DT_1BPB101": "c2",
    "INH_1B8000F_01": None,
}
# 지역 분류 칸 이름에 들어가는 말 (적재한 722개 표의 분류 이름 기준, 2026-11-11)
REGION_WORDS = re.compile(r"행정구역|시도|시군구|읍면동|동읍면|거주지|전입지|전출지|지역")
# 지역이라는 말이 있어도 국내 지역이 아닌 칸 (국적(지역)별, 외국인 아내의 국적별 …)
NOT_REGION_WORDS = re.compile(r"국적")

# 예전 시도 이름 → 현재 이름 (다른 표의 지역 경로를 맞출 때 사용)
OLD_SIDO_NAMES = {"강원도": "강원특별자치도", "전라북도": "전북특별자치도"}
# 전국 합계를 뜻하는 지역 이름
TOTAL_NAMES = {"전국", "계", "합계"}
# 표마다 다르게 쓰는 지역 경로 (공백 제거한 '시도>시군구' 형태)
PATH_FIXES = {"세종특별자치시>세종시": "세종특별자치시>세종특별자치시"}

MAP_COLUMNS = [
    "tbl_id",
    "src_dim",
    "src_code",
    "src_name",
    "src_path",
    "region_code",
    "match_method",
    "unmatched_reason",
    "valid_from",
    "valid_to",
]


def norm(text: str | None) -> str:
    """지역 이름 비교용: 공백 제거 ('중  구' == '중구')"""
    return re.sub(r"\s+", "", text or "")


def find_region_dim(tbl_id: str, dims: list[tuple[int, str]]) -> int | None:
    """지역 분류 칸 번호 (c1 → 1). dims는 (칸 번호, 분류 이름). 없으면 None"""
    if tbl_id in REGION_COLUMN:
        col = REGION_COLUMN[tbl_id]
        return int(col[1:]) if col else None
    for dim_no, name in sorted(dims):
        if REGION_WORDS.search(name or "") and not NOT_REGION_WORDS.search(name or ""):
            return dim_no
    return None


@dataclass(frozen=True)
class Source:
    """표의 지역 칸에 들어 있는 코드 하나"""

    code: str
    name: str | None
    level: int | str | None
    path: str | None


class RegionIndex:
    """기준 지역 목록(regions)을 연결용으로 정리한 것. rows는 (코드, 이름, 종류, 경로)"""

    def __init__(self, rows: list[tuple[str, str, str, str | None]]) -> None:
        self.name_by_code = {code: norm(name) for code, name, _, _ in rows}
        self.codes_by_path: dict[str, list[str]] = {}
        for code, _, _, path in rows:
            self.codes_by_path.setdefault(norm(path), []).append(code)
        self.sido_by_name = {norm(name): code for code, name, kind, _ in rows if kind == "시도"}


def match_region(idx: RegionIndex, org_id: str, src: Source) -> tuple[list[str], str | None]:
    """원래 지역 → (기준 코드 후보, 연결 방식)

    통계청 표 중에도 예전 코드 체계를 쓰는 표가 있다 (예전 31=경기도, 현재 31=울산광역시).
    그래서 코드만 보고 연결하지 않고, 아래 순서로 이름까지 확인한다.
      total : 전국/계/합계
      code  : 코드가 같고 이름도 같음 (통계청 표만)
      path  : '시도 > 시군구' 이름 경로가 같음 (3단계 경로는 시도와 마지막 이름만 사용)
      sido  : 시도 이름이 같음 (예전 시도 이름 포함)
    같은 경로에 코드가 둘 이상이면(예: 부천시 원미구 41195 → 41192) 후보를 모두 돌려준다.
    서비스는 유효 연도(valid_from, valid_to)로 하나를 고른다.
    """
    name = norm(src.name)
    if name in TOTAL_NAMES and str(src.level) == "1":
        return ["00"], "total"
    if org_id == "101" and idx.name_by_code.get(src.code) == name:
        return [src.code], "code"
    parts = [norm(OLD_SIDO_NAMES.get(p.strip(), p.strip())) for p in (src.path or "").split(">")]
    parts = [p for p in parts if p and p not in TOTAL_NAMES]
    if len(parts) >= 2:
        key = PATH_FIXES.get(f"{parts[0]}>{parts[-1]}", f"{parts[0]}>{parts[-1]}")
        if key in idx.codes_by_path:
            return idx.codes_by_path[key], "path"
    if len(parts) == 1 and parts[0] in idx.sido_by_name:
        return [idx.sido_by_name[parts[0]]], "sido"
    return [], None


def unmatched_reason(name: str | None) -> str:
    name = name or ""
    if "변동전" in name:
        return "변동전 지역 (현재 지역과 범위가 달라 연결하지 않음)"
    if norm(name) in {"동부", "읍부", "면부"}:
        return "지역이 아닌 동·읍·면부 분류"
    if "국외" in name:
        return "국외"
    return "기준 지역 목록(2008~2025 주민등록)에 없는 지역"


def map_rows(
    idx: RegionIndex, tbl_id: str, org_id: str, src_dim: int, sources: list[Source]
) -> list[dict]:
    """표 하나의 대응 줄. 연결한 코드는 후보마다 한 줄, 못 한 코드는 사유와 함께 한 줄"""
    rows = []
    for src in sources:
        codes, method = match_region(idx, org_id, src)
        base = {
            "tbl_id": tbl_id,
            "src_dim": src_dim,
            "src_code": src.code,
            "src_name": src.name,
            "src_path": src.path,
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
                    "unmatched_reason": unmatched_reason(src.name),
                }
            )
    return rows


# ---- DB -------------------------------------------------------------------------------------


def load_index(pg: psycopg.Connection) -> RegionIndex:
    return RegionIndex(
        pg.execute("SELECT region_code, region_name, kind, path FROM mart.regions").fetchall()
    )


def map_table(pg: psycopg.Connection, tbl_id: str, idx: RegionIndex | None = None) -> int:
    """표 하나의 region_code_map을 새로 만든다. 돌려주는 값은 기준 지역과 이어진 원래 코드 수

    지역 칸이 없거나 하나도 이어지지 않으면 그 표의 대응을 지우고 0을 돌려준다 (전국 값만 조회).
    """
    found = pg.execute(
        "SELECT tbl_key, org_id FROM catalog.stat_table WHERE tbl_id = %s", [tbl_id]
    ).fetchone()
    if found is None:
        raise ValueError(f"{tbl_id}: catalog.stat_table에 없는 표입니다")
    tbl_key, org_id = found
    dims = pg.execute(
        "SELECT dim_no, name FROM catalog.stat_dimension WHERE tbl_key = %s", [tbl_key]
    ).fetchall()
    src_dim = find_region_dim(tbl_id, dims)
    rows: list[dict] = []
    if src_dim is not None:
        sources = [
            Source(*r)
            for r in pg.execute(
                "SELECT code, name, level, path FROM catalog.stat_code"
                " WHERE tbl_key = %s AND dim_no = %s ORDER BY code",
                [tbl_key, src_dim],
            ).fetchall()
        ]
        rows = map_rows(idx or load_index(pg), tbl_id, org_id, src_dim, sources)
    matched = len({r["src_code"] for r in rows if r["region_code"]})
    with pg.transaction():
        pg.execute("DELETE FROM mart.region_code_map WHERE tbl_id = %s", [tbl_id])
        if matched:
            _insert(pg, rows)
    if src_dim is None:
        logger.info("%s: 지역 분류 없음 (전국 값만 조회)", tbl_id)
    elif not matched:
        logger.warning("%s: c%s 칸이 기준 지역과 이어지지 않아 대응 없음", tbl_id, src_dim)
    else:
        logger.info("%s: 지역 c%s, 이어진 코드 %d / %d", tbl_id, src_dim, matched, len(sources))
    return matched


def _insert(pg: psycopg.Connection, rows: list[dict]) -> None:
    """유효 연도는 기준 지역(regions)에서 가져온다"""
    names = ", ".join(MAP_COLUMNS[:8])
    with pg.cursor() as cur:
        cur.execute(
            "CREATE TEMP TABLE IF NOT EXISTS region_map_new"
            " (LIKE mart.region_code_map INCLUDING DEFAULTS)"
        )
        cur.execute("TRUNCATE region_map_new")
        with cur.copy(f"COPY region_map_new ({names}) FROM STDIN") as copy:
            for r in rows:
                copy.write_row([r[c] for c in MAP_COLUMNS[:8]])
        cur.execute(f"""
            INSERT INTO mart.region_code_map ({", ".join(MAP_COLUMNS)})
            SELECT {", ".join(f"n.{c}" for c in MAP_COLUMNS[:8])}, g.valid_from, g.valid_to
            FROM region_map_new n LEFT JOIN mart.regions g USING (region_code)
        """)


def missing_tables(pg: psycopg.Connection) -> list[str]:
    """적재했는데(raw.stat_load) 지역 대응이 없는 표"""
    return [
        r[0]
        for r in pg.execute("""
            SELECT t.tbl_id FROM raw.stat_load l JOIN catalog.stat_table t USING (tbl_key)
            WHERE NOT EXISTS (SELECT 1 FROM mart.region_code_map m WHERE m.tbl_id = t.tbl_id)
            ORDER BY t.tbl_id
        """).fetchall()
    ]


def main(argv: list[str] | None = None) -> None:
    import psycopg

    from pipeline.stats_build import conninfo

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tbl_ids", nargs="*", help="표 ID (없으면 대응이 없는 적재된 표 전부)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(message)s")

    with psycopg.connect(conninfo(), autocommit=True) as pg:
        targets = args.tbl_ids or missing_tables(pg)
        idx = load_index(pg)
        mapped = none = 0
        for i, tbl_id in enumerate(targets, start=1):
            if map_table(pg, tbl_id, idx):
                mapped += 1
            else:
                none += 1
            if i % 100 == 0:
                print(f"  {i}/{len(targets)}")
    print(f"지역 대응: 대상 {len(targets)}개 → 만듦 {mapped}개, 지역 없음·연결 안 됨 {none}개")


if __name__ == "__main__":
    main()
