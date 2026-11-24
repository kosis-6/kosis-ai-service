"""동적 지표 계산 방법 캐시 (동적 지표 4단계, 표 app.dynamic_plans, 마이그레이션 0018)

사전에 없는 지표를 통계표에서 바로 계산했으면 어느 표로(칸 고르기 LLM이 고른 칸까지) 계산했는지
남긴다. 되묻기에서 사람이 고른 칸은 남기지 않는다 (모두의 기본값이 되지 않게).
LLM이 고른 칸은 그 칸을 고른 근거 낱말(words)과 함께 남기고, 다시 쓸 때 새 질문에 맞는 칸만 쓴다
(dynamic_indicator.reusable_forced: 지표 표현만 같은 다른 질문에 예전 칸을 쓰지 않게).
같은 지표를 다시 물으면 통계표 찾기(HCX 3회, 십여 초)를 건너뛰고 그 표로 바로 계산한다.
칸 고르기·검증은 매번 다시 한다 (DB 조회만이라 빠르다). 그래서
  - 표가 다시 적재돼 코드가 바뀌어도, 질문에 다른 조건이 붙어도 그 질문에 맞는 칸을 고른다
  - 그 표로 더 이상 계산되지 않으면 쓰지 않고 지금처럼 통계표를 찾는다
지역·연도와 숫자는 저장하지 않는다 (숫자는 매일 0시 갱신으로 바뀔 수 있다).

키: 지표 표현(HCX 해석의 term·indicator)에서 공백·기호를 뺀 것. 사전 지표 이름이거나 연도·지역이
섞인 표현(예: "2020년 서울")이면 저장하지 않는다. 표현이 다르면 따로 저장된다
("1인 가구 수", "혼자 사는 가구 수"를 묶는 것은 2단계 LLM 보조).

DB가 원본이고 서버 메모리(memory_cache)에 키별 계획을 사본으로 둔다.
KEEP_DAYS 동안 쓰이지 않은 계획은 저장할 때 지운다.
"""

import json
import logging
import re

from sqlalchemy import text

from app.services import indicators
from app.services.memory_cache import MISSING, MemoryCache

logger = logging.getLogger(__name__)

KEEP_DAYS = 90
CACHE = MemoryCache("dynamic_plans", maxsize=512, ttl=600)
NORM = re.compile(r"[\s·,.()\[\]{}<>\"'‘’“”_/-]")


def key_of(parsed: dict) -> tuple[str, str] | None:
    """(키, 지표 표현). 저장할 수 없는 표현이면 None
    DB의 지역 이름을 읽으므로 스레드에서 부른다"""
    for term in (parsed.get("term"), parsed.get("indicator")):
        if not term or not isinstance(term, str):
            continue
        # "전국 1인 가구 수"와 "1인 가구 수"는 같은 지표다 (범위는 지역 조건이 맡는다)
        term = indicators.without_national(term)
        if indicators.canonical_name(term) or re.search(r"\d{4}", term):
            continue
        if indicators.find_regions(term):
            continue
        key = NORM.sub("", term)
        if len(key) >= 2:
            return key, term
    return None


async def find(key: str) -> list[dict]:
    """키의 계획들 (많이 쓴 것부터). 메모리에 있으면 DB를 읽지 않는다"""
    cached = CACHE.get(key)
    if cached is not MISSING:
        return cached
    from app.core.db import engine

    async with engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id, key, term, tbl_id, forced, words, name, formula, cells, uses"
                        " FROM app.dynamic_plans WHERE key = :key"
                        " ORDER BY uses DESC, last_used_at DESC"
                    ),
                    {"key": key},
                )
            )
            .mappings()
            .all()
        )
    plans = [dict(r) for r in rows]
    CACHE.set(key, plans)
    return plans


async def save(key: str, term: str, tbl_id: str, forced: dict, spec: dict, words=()) -> None:
    """바로 계산에 성공한 계획을 남긴다. 이미 있으면 쓴 횟수만 올린다
    words: 칸 고르기 LLM이 forced의 칸을 고른 근거 [(질문 낱말, 표 낱말)] (마이그레이션 0020)"""
    from app.core.db import engine

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO app.dynamic_plans"
                " (key, term, tbl_id, forced, words, name, formula, cells)"
                " VALUES (:key, :term, :tbl_id, CAST(:forced AS jsonb), CAST(:words AS jsonb),"
                " :name, :formula, :cells)"
                " ON CONFLICT ON CONSTRAINT dynamic_plans_unique DO UPDATE"
                " SET uses = app.dynamic_plans.uses + 1, last_used_at = now(),"
                " words = EXCLUDED.words,"
                " name = EXCLUDED.name, formula = EXCLUDED.formula, cells = EXCLUDED.cells"
            ),
            {
                "key": key,
                "term": term,
                "tbl_id": tbl_id,
                "forced": json.dumps({str(k): v for k, v in (forced or {}).items()}),
                "words": json.dumps([list(w) for w in words or []], ensure_ascii=False),
                "name": spec.get("name") or term,
                "formula": spec.get("formula"),
                "cells": spec.get("cells"),
            },
        )
        await conn.execute(
            text(
                "DELETE FROM app.dynamic_plans"
                " WHERE last_used_at < now() - make_interval(days => :days)"
            ),
            {"days": KEEP_DAYS},
        )
    CACHE.delete(key)


async def used(plan_id: int) -> None:
    """캐시에서 꺼내 계산했다 (쓴 횟수·마지막으로 쓴 시각)"""
    from app.core.db import engine

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE app.dynamic_plans SET uses = uses + 1, last_used_at = now() WHERE id = :id"
            ),
            {"id": plan_id},
        )


async def drop(plan_id: int) -> bool:
    """계획 하나를 지운다 (관리자). 없으면 False"""
    from app.core.db import engine

    async with engine.begin() as conn:
        key = (
            await conn.execute(
                text("DELETE FROM app.dynamic_plans WHERE id = :id RETURNING key"),
                {"id": plan_id},
            )
        ).scalar()
    if key is None:
        return False
    CACHE.delete(key)
    return True


async def listing(limit: int = 50, offset: int = 0) -> dict:
    """계획 목록 (많이 쓴 것부터). 지표 사전에 올릴 후보를 고를 때 본다"""
    from app.core.db import engine

    async with engine.begin() as conn:
        total = (await conn.execute(text("SELECT count(*) FROM app.dynamic_plans"))).scalar()
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT id, term, tbl_id, forced, name, formula, cells, uses,"
                        " created_at, last_used_at FROM app.dynamic_plans"
                        " ORDER BY uses DESC, last_used_at DESC LIMIT :limit OFFSET :offset"
                    ),
                    {"limit": limit, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
    return {"total": total, "items": [dict(r) for r in rows], "memory": CACHE.stats()}
