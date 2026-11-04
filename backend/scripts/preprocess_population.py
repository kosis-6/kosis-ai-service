# /// script
# requires-python = ">=3.12"
# dependencies = ["xlrd==2.0.2"]
# ///
"""역할: 인구 통계표 XLS를 preprecess.json 형식의 중복 없는 Catalog로 변환한다.
Data In: data/catalog/population.xls의 표 목록과 셀 하이퍼링크 (KOSIS 통계표 목록 다운로드 파일).
Data Out: data/catalog/population_table_catalog.jsonl. 원본 XLS와 템플릿은 변경하지 않는다.
실행: 프로젝트 루트에서 uv run scripts/preprocess_population.py
다음 단계: python -m scripts.enrich_catalog_meta (분류·항목 메타데이터 추가)
"""

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import xlrd
from xlrd.sheet import Sheet

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "data" / "catalog" / "population.xls"
OUTPUT = ROOT / "data" / "catalog" / "population_table_catalog.jsonl"
HEADERS = ["Level", "통계명", "통계표조회", "출처", "수록기간", "경로",
           "경로(LIST_ID)", "통계표 아이디(TBL_ID)"]


def parse_periods(text: str) -> list[dict[str, str]]:
    """역할: 복수 주기를 분리하고 주기명과 시점 원문을 보존한다.
    Data In: '년 (1992 ~ 2025) , 월 (201101 ~ 202609)' 형태의 문자열.
    Data Out: frequency_label, start, end를 가진 객체 목록.
    """
    periods: list[dict[str, str]] = []
    for part in text.split(","):
        match = re.fullmatch(r"\s*([^(),]+?)\s*\(\s*(\d+)\s*~\s*(\d+)\s*\)\s*", part)
        if not match:
            raise ValueError(f"수록기간 형식 확인 필요: {part!r}")
        label, start, end = match.groups()
        if (not label.strip() or len(start) not in (4, 6, 8)
                or len(start) != len(end) or start > end):
            raise ValueError(f"수록기간 범위 확인 필요: {part!r}")
        periods.append({"frequency_label": label.strip(), "start": start, "end": end})
    return periods


def parse_table(sheet: Sheet, row_index: int, url: str) -> dict:
    """역할: 표 한 행을 변환하고 링크 식별자와 분류 경로를 대조한다.
    Data In: XLS 시트, 0부터 시작하는 행 위치, 해당 셀의 실제 하이퍼링크.
    Data Out: preprecess.json과 같은 필드를 가진 통계표 객체. 불일치 시 예외가 발생한다.
    """
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
        "paths": [{
            "level": level,
            "path": path,
            "list_id_path": list_ids,
            "list_id": parameters["list_id"][0],
            "kosis_table_url": url,
            "source_row": row_index + 1,
        }],
    }


def read_catalog(source: Path) -> tuple[list[dict], int]:
    """역할: 폴더 행을 제외하고 같은 기관·표의 모든 메뉴 경로를 모은다.
    Data In: population.xls 경로.
    Data Out: 원본 순서의 고유 통계표 목록과 중복을 포함한 통계표 행 수.
    """
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
    """역할: 검증된 결과를 UTF-8 JSONL로 저장한다.
    Data In: 통계표 목록과 저장 경로.
    Data Out: 한 줄에 통계표 객체 하나를 담은 파일. 저장 완료 후 최종 파일을 교체한다.
    """
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
    """역할: 주기가 합쳐지거나 잘못된 수록기간이 통과하는 회귀를 검사한다.
    Data In: 연·월·분기·부정기 및 잘못된 기간 예제.
    Data Out: 정상 시 반환값 없음. 결과가 다르면 검사 실패.
    """
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
    """역할: 자체 검사, XLS 변환, JSONL 저장을 순서대로 실행한다.
    Data In: SOURCE 상수의 원본 파일.
    Data Out: OUTPUT 파일과 처리 건수 요약. KOSIS·임베딩 API는 호출하지 않는다.
    """
    check_period_parser()
    records, table_rows = read_catalog(SOURCE)
    write_catalog(records, OUTPUT)
    print(json.dumps({
        "output": str(OUTPUT.relative_to(ROOT)),
        "table_rows": table_rows,
        "unique_tables": len(records),
        "merged_duplicate_rows": table_rows - len(records),
        "survey_count": len({record["survey_name"] for record in records}),
        "organization_code_count": len({record["org_id"] for record in records}),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
