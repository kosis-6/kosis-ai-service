import copy
import re
from contextvars import ContextVar
from typing import Any

VAGUE_REGION = re.compile(r"^(우리\s*(구|군|시|동네|지역|마을|읍|면|동)|여기|이\s*동네)$")
NUMERIC_DEFAULTS = {"level": "시도", "sido": None, "top": None, "ascending": False, "by": None}

_kept: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar("merged_slots", default=None)


def _key(question: str) -> str:
    return re.sub(r"\s+", " ", question or "").strip()


def split(obj: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    com = obj.get("common") if isinstance(obj.get("common"), dict) else {}
    sea = obj.get("search") if isinstance(obj.get("search"), dict) else None
    num = obj.get("numeric") if isinstance(obj.get("numeric"), dict) else None
    regions = [r for r in (com.get("regions") or []) if isinstance(r, str) and r.strip()]
    year, year_end, sex = com.get("year"), com.get("year_end"), com.get("sex")
    numeric = None
    if num is not None:
        numeric = {**NUMERIC_DEFAULTS, **{k: v for k, v in num.items() if v is not None}}
        if year_end:
            years = {"year": year_end, "start_year": year, "end_year": year_end}
        elif year and numeric.get("type") == "trend":
            years = {"year": None, "start_year": year, "end_year": None}
        else:
            years = {"year": year, "start_year": None, "end_year": None}
        numeric.update(years, regions=regions, sex=sex if sex in ("남", "여") else "계")
    search = None
    if sea is not None:
        plain = [r for r in regions if not VAGUE_REGION.match(_key(r))]
        search = {
            k: sea.get(k) for k in ("topic", "search_query", "target", "exclude", "query_type")
        }
        search["conditions"] = {
            "classes": sea.get("classes") or [],
            "items": sea.get("items") or [],
            "sex": sex,
            "region": plain[0] if len(plain) == 1 else None,
            "region_level": com.get("region_level"),
            "year": year,
            "year_end": year_end,
        }
    return numeric, search


def start() -> None:
    _kept.set({})


def keep(question: str, slots: dict[str, Any]) -> None:
    kept = _kept.get()
    if kept is None:
        kept = {}
        _kept.set(kept)
    kept[_key(question)] = copy.deepcopy(slots)


def take(question: str) -> dict[str, Any] | None:
    found = (_kept.get() or {}).get(_key(question))
    return copy.deepcopy(found) if found is not None else None
