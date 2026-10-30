"""통계표 검색 문서 만들기 (실전 2 F-02, 검색 규칙 3장)

임베딩할 표 단위 문서. 노트북(모델 비교)·검색 색인·점검 스크립트가 같은 문서를 쓰도록
여기서만 만든다.

문서 구성 (연구계획서 E3 필드 비교용 단계)
  name     조사명 | 표명
  classes  + 분류명
  full     + 분류 값 | 항목명 | 경로 (기능 정의서 F-02 초안)
           - 범용 단어(계, 합계, 기타, 미상, 남자, 여자 …)는 뺀다
           - 경로의 공통 앞부분 '주제별통계 > 인구 > '와 마지막 단계(표명과 같음)는 뺀다
           - 주석 요약(대상 정의·기준일)은 아직 넣지 않는다

입력은 load_catalog.clean_record 결과나 stat_tables 행 (table_name, survey_name, classifications,
items, path_texts 필드가 있으면 된다).
"""

from typing import Any

PRESETS = ("name", "classes", "full")
GENERIC = {"계", "합계", "소계", "총계", "전체", "기타", "미상", "남자", "여자", "남", "여"}
PATH_PREFIX = "주제별통계 > 인구 > "


def _unique(values: list[str]) -> list[str]:
    return [v for v in dict.fromkeys(v.strip() for v in values if v) if v and v not in GENERIC]


def build(t: dict[str, Any], preset: str = "full") -> str:
    if preset not in PRESETS:
        raise ValueError(f"preset은 {PRESETS} 중 하나: {preset}")
    classes = t.get("classifications") or []
    parts = [t["survey_name"], t["table_name"]]
    if preset in ("classes", "full"):
        parts.append(" ".join(_unique([c["name"] for c in classes])))
    if preset == "full":
        values = [v for c in classes for v in c.get("sample_values") or []]
        items = [i["name"] for i in t.get("items") or []]
        folders = [
            p.removeprefix(PATH_PREFIX).rsplit(" > ", 1)[0]
            for p in t.get("path_texts") or []
            if " > " in p.removeprefix(PATH_PREFIX)
        ]
        parts += [" ".join(_unique(values)), " ".join(_unique(items)), " ".join(_unique(folders))]
    return " | ".join(p for p in parts if p)
