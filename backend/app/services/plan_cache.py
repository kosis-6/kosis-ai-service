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
    for term in (parsed.get("term"), parsed.get("indicator")):
        if not term or not isinstance(term, str):
            continue
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
    from app.core.db import engine

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE app.dynamic_plans SET uses = uses + 1, last_used_at = now() WHERE id = :id"
            ),
            {"id": plan_id},
        )


async def drop(plan_id: int) -> bool:
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
