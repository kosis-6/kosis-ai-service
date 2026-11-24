from typing import Any

from sqlalchemy import text

from app.services.log_redaction import mask

TITLE_MAX = 60
REPLAY_KEYS = (
    "status",
    "answer",
    "chart",
    "tables",
    "notice",
    "applied",
    "request_type",
    "clarification",
    "summary",
    "sources",
)


def title_of(question: str) -> str:
    text_ = " ".join((mask(question) or "").split())
    return text_ if len(text_) <= TITLE_MAX else text_[: TITLE_MAX - 1] + "…"


def replay(res: dict) -> dict:
    out = {k: res.get(k) for k in REPLAY_KEYS if res.get(k) is not None}
    if isinstance(out.get("clarification"), dict):
        out["clarification"] = {k: v for k, v in out["clarification"].items() if k != "context"}
    return out


async def touch(session_id: str, user_id: int, question: str) -> None:
    from app.core.db import engine

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "insert into app.conversations (session_id, user_id, title)"
                " values (:sid, :uid, :title)"
                " on conflict (session_id) do update set updated_at = now()"
                " where app.conversations.user_id = excluded.user_id"
            ),
            {"sid": session_id, "uid": user_id, "title": title_of(question)},
        )


async def listing(user_id: int, limit: int = 30, offset: int = 0) -> dict[str, Any]:
    from app.core.db import engine

    async with engine.begin() as conn:
        total = (
            await conn.execute(
                text("select count(*) from app.conversations where user_id = :uid"),
                {"uid": user_id},
            )
        ).scalar()
        rows = (
            (
                await conn.execute(
                    text(
                        "select c.session_id, c.title, c.created_at, c.updated_at,"
                        " (select count(*) from app.chat_turns t"
                        "   where t.session_id = c.session_id and t.user_id = c.user_id) as turns"
                        " from app.conversations c where c.user_id = :uid"
                        " order by c.updated_at desc limit :limit offset :offset"
                    ),
                    {"uid": user_id, "limit": limit, "offset": offset},
                )
            )
            .mappings()
            .all()
        )
    return {"total": total, "conversations": [dict(r) for r in rows]}


async def detail(user_id: int, session_id: str) -> dict[str, Any] | None:
    from app.core.db import engine

    async with engine.begin() as conn:
        head = (
            (
                await conn.execute(
                    text(
                        "select session_id, title, created_at, updated_at from app.conversations"
                        " where session_id = :sid and user_id = :uid"
                    ),
                    {"sid": session_id, "uid": user_id},
                )
            )
            .mappings()
            .first()
        )
        if head is None:
            return None
        turns = (
            (
                await conn.execute(
                    text(
                        "select turn_no, message as question, response, answer, created_at"
                        " from app.chat_turns where session_id = :sid and user_id = :uid"
                        " order by turn_no, id"
                    ),
                    {"sid": session_id, "uid": user_id},
                )
            )
            .mappings()
            .all()
        )
    items = []
    for t in turns:
        response = t["response"] or {"answer": t["answer"]}
        items.append(
            {
                "turn_no": t["turn_no"],
                "question": t["question"],
                "created_at": t["created_at"],
                "response": response,
            }
        )
    return {**dict(head), "turns": items}


async def rename(user_id: int, session_id: str, title: str) -> bool:
    from app.core.db import engine

    async with engine.begin() as conn:
        done = await conn.execute(
            text(
                "update app.conversations set title = :title"
                " where session_id = :sid and user_id = :uid"
            ),
            {"sid": session_id, "uid": user_id, "title": title_of(title)},
        )
    return done.rowcount > 0


async def delete(user_id: int, session_id: str) -> bool:
    from app.core.db import engine
    from app.services import ask_session

    async with engine.begin() as conn:
        gone = await conn.execute(
            text("delete from app.conversations where session_id = :sid and user_id = :uid"),
            {"sid": session_id, "uid": user_id},
        )
        if gone.rowcount == 0:
            return False
        await conn.execute(
            text("delete from app.chat_turns where session_id = :sid and user_id = :uid"),
            {"sid": session_id, "uid": user_id},
        )
        await conn.execute(
            text("delete from app.chat_sessions where session_id = :key"),
            {"key": ask_session._key(session_id)},
        )
    ask_session.STATE_CACHE.delete(session_id)
    return True
