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
