"""질문 이해 + 조건 추출을 Gemma 한 번으로 (실험 2-A, UNDERSTAND_MERGED=gemma)

qa.understand 가 합친 프롬프트(app/prompts/understand_merged.py)로 Gemma 를 한 번 부르고, 출력을
지금의 두 모양으로 나눈다 (split).
  numeric → 질문 이해(HCX) 출력 모양 → qa.correct
  search  → 조건 추출 출력 모양 → slots.normalize · slots.correct → 이 요청에 보관 (keep)
통계표 찾기가 같은 질문으로 slots.extract 를 부르면 Gemma 를 다시 부르지 않고
보관한 것을 쓴다 (take).
보관은 요청마다 따로(ContextVar, flow · llm_calls 와 같은 방식)라 다른 질문과 섞이지 않는다.
이 모듈은 qa · slots 를 가져오지 않는다 (둘이 이 모듈을 쓴다).
"""

import copy
import re
from contextvars import ContextVar
from typing import Any

# 수치 쪽은 '우리 구'를 지역 표현으로 남기고(되묻기로 이어짐), 통계표 찾기 쪽은 지역이 아니다
# (SLOT_SYSTEM_GEMMA 의 region 규칙). 공통 칸은 수치 쪽처럼 받고 통계표 찾기로 넘길 때만 뺀다
VAGUE_REGION = re.compile(r"^(우리\s*(구|군|시|동네|지역|마을|읍|면|동)|여기|이\s*동네)$")
NUMERIC_DEFAULTS = {"level": "시도", "sido": None, "top": None, "ascending": False, "by": None}

_kept: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar("merged_slots", default=None)


def _key(question: str) -> str:
    return re.sub(r"\s+", " ", question or "").strip()


def split(obj: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """합친 출력 → (질문 이해 출력 모양, 조건 추출 출력 모양). 블록이 없으면 그쪽은 None"""
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
            # "2015년부터 지금까지"에서 끝이 빠진 경우: 한 해로 바꾸지 않는다 (계산이 마지막 해까지)
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
    """이 요청(/ask 한 턴)의 보관함을 새로 만든다. 턴 처음에 만들어 두고 keep 은 채우기만 해서,
    질문 이해가 따로 도는 작업(task) 안에서 보관해도 같은 턴의 통계표 찾기가 본다"""
    _kept.set({})


def keep(question: str, slots: dict[str, Any]) -> None:
    """이 요청의 조건 추출 결과로 보관한다 (slots.extract 가 같은 질문이면 꺼내 쓴다)"""
    kept = _kept.get()
    if kept is None:  # start() 없이 부른 곳(스크립트 · 테스트): 지금 작업 안에서만 쓴다
        kept = {}
        _kept.set(kept)
    kept[_key(question)] = copy.deepcopy(slots)


def take(question: str) -> dict[str, Any] | None:
    """보관한 조건 추출 결과 (같은 질문일 때만). 없으면 None"""
    found = (_kept.get() or {}).get(_key(question))
    return copy.deepcopy(found) if found is not None else None
