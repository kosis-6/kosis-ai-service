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

REGION_COLUMN = {
    "DT_1BPB002": "c2",
    "DT_1BPB003": "c2",
    "DT_1BPB101": "c2",
    "INH_1B8000F_01": None,
}
REGION_WORDS = re.compile(r"행정구역|시도|시군구|읍면동|동읍면|거주지|전입지|전출지|지역")
NOT_REGION_WORDS = re.compile(r"국적")

OLD_SIDO_NAMES = {
    "강원도": "강원특별자치도",
    "전라북도": "전북특별자치도",
    "제주도": "제주특별자치도",
}
OLD_SIDO_SIGUNGU_SKIP = {"제주도"}
TOTAL_NAMES = {"전국", "계", "합계", "총계"}
PATH_FIXES = {
    "세종특별자치시>세종시": "세종특별자치시>세종특별자치시",
    "경상남도>통합창원시": "경상남도>창원시(통합)",
    "경기도>포천군": "경기도>포천시",
    "경기도>양주군": "경기도>양주시",
    "경기도>광주군": "경기도>광주시",
    "경기도>화성군": "경기도>화성시",
    "경기도>김포군": "경기도>김포시",
    "경기도>안성군": "경기도>안성시",
    "경기도>용인군": "경기도>용인시",
    "경기도>이천군": "경기도>이천시",
    "경기도>파주군": "경기도>파주시",
    "충청남도>논산군": "충청남도>논산시",
    "경상남도>양산군": "경상남도>양산시",
    "경기도>강화군": "인천광역시>강화군",
    "경기도>옹진군": "인천광역시>옹진군",
    "경상북도>달성군": "대구광역시>달성군",
}
OLD_NAME_ONLY = {
    "포천군": "경기도>포천시",
    "양주군": "경기도>양주시",
    "광주군": "경기도>광주시",
    "화성군": "경기도>화성시",
}
CITY_GU = re.compile(r"^.+시(.+구)$")
TOWN_END = re.compile(r"(읍|면|동|가|리)$")

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
    return re.sub(r"\s+", "", text or "")


def find_region_dim(tbl_id: str, dims: list[tuple[int, str]]) -> int | None:
    if tbl_id in REGION_COLUMN:
        col = REGION_COLUMN[tbl_id]
        return int(col[1:]) if col else None
    for dim_no, name in sorted(dims):
        if REGION_WORDS.search(name or "") and not NOT_REGION_WORDS.search(name or ""):
            return dim_no
    return None


@dataclass(frozen=True)
class Source:
    code: str
    name: str | None
    level: int | str | None
    path: str | None


class RegionIndex:
    def __init__(self, rows: list[tuple[str, str, str, str | None]]) -> None:
        self.name_by_code = {code: norm(name) for code, name, _, _ in rows}
        self.codes_by_path: dict[str, list[str]] = {}
        for code, _, _, path in rows:
            self.codes_by_path.setdefault(norm(path), []).append(code)
        self.sido_by_name = {norm(name): code for code, name, kind, _ in rows if kind == "시도"}


def match_region(idx: RegionIndex, org_id: str, src: Source) -> tuple[list[str], str | None]:
    name = norm(src.name)
    if name in TOTAL_NAMES and str(src.level) == "1":
        return ["00"], "total"
    raw = [p.strip() for p in (src.path or "").split(">") if p.strip()]
    if len(raw) >= 2 and norm(raw[0]) in OLD_SIDO_SIGUNGU_SKIP:
        return [], None
    if org_id == "101" and idx.name_by_code.get(src.code) == name:
        return [src.code], "code"
    parts = [norm(OLD_SIDO_NAMES.get(p.strip(), p.strip())) for p in (src.path or "").split(">")]
    parts = [p for p in parts if p and p not in TOTAL_NAMES]
    if len(parts) >= 2:
        key = PATH_FIXES.get(f"{parts[0]}>{parts[-1]}", f"{parts[0]}>{parts[-1]}")
        if key in idx.codes_by_path:
            return idx.codes_by_path[key], "path"
        if m := CITY_GU.match(parts[-1]):
            key = f"{parts[0]}>{m[1]}"
            if key in idx.codes_by_path:
                return idx.codes_by_path[key], "path"
        if len(parts) == 2 and parts[-1] in idx.sido_by_name:
            return [idx.sido_by_name[parts[-1]]], "sido"
    if len(parts) == 1:
        if parts[0] in idx.sido_by_name:
            return [idx.sido_by_name[parts[0]]], "sido"
        key = OLD_NAME_ONLY.get(parts[0])
        if key in idx.codes_by_path:
            return idx.codes_by_path[key], "path"
    return [], None


def unmatched_reason(name: str | None, path: str | None = None) -> str:
    parts = [norm(p) for p in (path or "").split(">") if p.strip()]
    if len(parts) >= 2 and parts[0] in OLD_SIDO_SIGUNGU_SKIP:
        return "변동전 지역 (현재 지역과 범위가 달라 연결하지 않음)"
    name = norm(name)
    if "변동전" in name:
        return "변동전 지역 (현재 지역과 범위가 달라 연결하지 않음)"
    if name in {"동부", "읍부", "면부", "읍면부"}:
        return "지역이 아닌 동·읍·면부 분류"
    if "국외" in name:
        return "국외"
    if name in {"기타", "미상"}:
        return "지역이 아닌 분류 (기타·미상)"
    if "출장" in name:
        return "출장소 (기준 목록은 시군구까지)"
    if TOWN_END.search(name):
        return "읍·면·동 (기준 목록은 시군구까지)"
    return "기준 지역 목록(2008~2025 주민등록)에 없는 지역"


def map_rows(
    idx: RegionIndex, tbl_id: str, org_id: str, src_dim: int, sources: list[Source]
) -> list[dict]:
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
                    "unmatched_reason": unmatched_reason(src.name, src.path),
                }
            )
    return rows


def load_index(pg: psycopg.Connection) -> RegionIndex:
    return RegionIndex(
        pg.execute("SELECT region_code, region_name, kind, path FROM mart.regions").fetchall()
    )


def map_table(pg: psycopg.Connection, tbl_id: str, idx: RegionIndex | None = None) -> int:
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
