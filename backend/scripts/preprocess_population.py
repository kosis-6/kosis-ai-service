import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import xlrd
from xlrd.sheet import Sheet

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "catalog" / "population.xls"
OUTPUT = ROOT / "data" / "catalog" / "population_table_catalog.jsonl"
HEADERS = [
    "Level",
    "통계명",
    "통계표조회",
    "출처",
    "수록기간",
    "경로",
    "경로(LIST_ID)",
    "통계표 아이디(TBL_ID)",
]


def parse_periods(text: str) -> list[dict[str, str]]:
    periods: list[dict[str, str]] = []
    for part in text.split(","):
        match = re.fullmatch(r"\s*([^(),]+?)\s*\(\s*(\d+)\s*~\s*(\d+)\s*\)\s*", part)
        if not match:
            raise ValueError(f"수록기간 형식 확인 필요: {part!r}")
        label, start, end = match.groups()
        if (
            not label.strip()
            or len(start) not in (4, 6, 8)
            or len(start) != len(end)
            or start > end
        ):
            raise ValueError(f"수록기간 범위 확인 필요: {part!r}")
        periods.append({"frequency_label": label.strip(), "start": start, "end": end})
    return periods


def parse_table(sheet: Sheet, row_index: int, url: str) -> dict:
    values = [str(value).strip() for value in sheet.row_values(row_index)]
    if not all(values):
        raise ValueError("통계표 행에 필수 값 누락")
    level_text, name, _, source, period, path, list_path_text, tbl_id = values
    level = int(level_text)
    address = urlsplit(url)
    if address.scheme not in ("http", "https") or address.hostname != "kosis.kr":
        raise ValueError("KOSIS 통계표 하이퍼링크 확인 필요")
    parameters = parse_qs(address.query)
    for key in ("orgId", "tblId", "list_id"):
        if len(parameters.get(key, [])) != 1:
            raise ValueError(f"하이퍼링크 식별자 누락 또는 중복: {key}")
    if parameters["tblId"][0] != tbl_id:
        raise ValueError("셀의 TBL_ID와 하이퍼링크의 tblId 불일치")

    source_match = re.fullmatch(r"(.+?),\s*「(.+?)」", source)
    if not source_match:
        raise ValueError(f"출처 형식 확인 필요: {source!r}")
    organization, survey_name = source_match.groups()
    path_names = path.split(" > ")
    list_ids = [part.strip() for part in list_path_text.split(">") if part.strip()]
    if path_names[:2] != ["주제별통계", "인구"]:
        raise ValueError("인구 주제 밖의 경로")
    if len(path_names) != level + 1 or path_names[-1] != name:
        raise ValueError("Level·통계명·경로 불일치")
    if not list_ids or len(list_ids) != level - 1 or list_ids[-1] != parameters["list_id"][0]:
        raise ValueError("LIST_ID 경로와 하이퍼링크 불일치")

    return {
        "org_id": parameters["orgId"][0],
        "tbl_id": tbl_id,
        "table_name": name,
        "survey_name": survey_name.strip(),
        "organization": organization.strip(),
        "topic": "인구",
        "source_text": source,
        "period": period,
        "catalog_periods": parse_periods(period),
        "paths": [
            {
                "level": level,
                "path": path,
                "list_id_path": list_ids,
                "list_id": parameters["list_id"][0],
                "kosis_table_url": url,
                "source_row": row_index + 1,
            }
        ],
    }


def read_catalog(source: Path) -> tuple[list[dict], int]:
    workbook = xlrd.open_workbook(source, formatting_info=True)
    try:
        sheet = workbook.sheet_by_name("Sheet1")
        if sheet.ncols != len(HEADERS) or sheet.row_values(3) != HEADERS:
            raise ValueError("Sheet1의 4행 헤더가 예상한 8개 열과 다릅니다.")
        links: dict[int, str] = {}
        for link in sheet.hyperlink_list:
            if link.fcolx != 2:
                continue
            if link.lcolx != 2 or link.frowx != link.lrowx or link.frowx in links:
                raise ValueError("통계표 조회 셀의 하이퍼링크 범위 확인 필요")
            links[link.frowx] = link.url_or_path

        catalog: dict[tuple[str, str], dict] = {}
        table_rows = 0
        for index in range(4, sheet.nrows):
            values = sheet.row_values(index)
            if not str(values[7]).strip():
                if index in links or any(values[column] for column in (2, 3, 4)):
                    raise ValueError(f"Excel {index + 1}행: TBL_ID가 없는 통계표 행")
                continue
            table_rows += 1
            try:
                if index not in links:
                    raise ValueError("통계표 하이퍼링크 누락")
                table = parse_table(sheet, index, links[index])
                key = (table["org_id"], table["tbl_id"])
                if key not in catalog:
                    catalog[key] = table
                else:
                    existing = catalog[key]
                    for field in table:
                        if field != "paths" and existing[field] != table[field]:
                            raise ValueError(f"중복 통계표 {key}의 {field} 값 충돌")
                    existing["paths"].extend(table["paths"])
            except ValueError as error:
                raise ValueError(f"Excel {index + 1}행: {error}") from error
        if not catalog:
            raise ValueError("인구 통계표가 없습니다.")
        records = list(catalog.values())
        assert sum(len(record["paths"]) for record in records) == table_rows, "원본 행 누락"
        return records, table_rows
    finally:
        workbook.release_resources()


def write_catalog(records: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def check_period_parser() -> None:
    assert parse_periods("년 (1992 ~ 2025) , 월 (201101 ~ 202609)") == [
        {"frequency_label": "년", "start": "1992", "end": "2025"},
        {"frequency_label": "월", "start": "201101", "end": "202609"},
    ]
    assert parse_periods("분기 (198101 ~ 202602)") == [
        {"frequency_label": "분기", "start": "198101", "end": "202602"},
    ]
    assert parse_periods("IR (2015 ~ 2015)")[0]["frequency_label"] == "IR"
    for invalid in ("년 (2020 ~ )", "년 (2025 ~ 2020)", "년 (2020 ~ 2025) 잘못된 값"):
        try:
            parse_periods(invalid)
        except ValueError:
            continue
        raise AssertionError(f"잘못된 기간을 허용했습니다: {invalid}")


def main() -> None:
    check_period_parser()
    records, table_rows = read_catalog(SOURCE)
    write_catalog(records, OUTPUT)
    print(
        json.dumps(
            {
                "output": str(OUTPUT.relative_to(ROOT)),
                "table_rows": table_rows,
                "unique_tables": len(records),
                "merged_duplicate_rows": table_rows - len(records),
                "survey_count": len({record["survey_name"] for record in records}),
                "organization_code_count": len({record["org_id"] for record in records}),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
