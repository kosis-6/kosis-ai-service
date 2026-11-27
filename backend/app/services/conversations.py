import json
from typing import Any

from sqlalchemy import text

from app.services.log_redaction import mask, redact

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
    "figure",
    "reportable",
    "report_ref",
    "talk_summary",
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


TOUCH = text(
    "insert into app.conversations (session_id, user_id, title)"
    " values (:sid, :uid, :title)"
    " on conflict (session_id) do update set updated_at = now()"
    " where app.conversations.user_id = excluded.user_id"
)


async def touch(session_id: str, user_id: int, question: str, conn=None) -> None:
    """대화 목록에 올리거나 갱신. conn이 있으면 그 트랜잭션 안에서 (한 턴의 저장을 함께 묶는다)"""
    params = {"sid": session_id, "uid": user_id, "title": title_of(question)}
    if conn is not None:
        await conn.execute(TOUCH, params)
        return
    from app.core.db import engine

    async with engine.begin() as own:
        await own.execute(TOUCH, params)


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
                        "select id, turn_no, message as question, response, answer, created_at"
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
        if response.get("reportable") or response.get("chart"):
            # 다시 연 답에서도 보고서에 담거나 차트 보기를 저장할 수 있게 한다
            response = {**response, "turn_id": t["id"]}
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


async def chart_view(user_id: int, session_id: str, turn_no: int, last: dict) -> str | None:
    """직전 차트를 그린 턴에서 최신 보기 설정을 읽는다."""
    from app.core.db import engine

    params = {"sid": session_id, "uid": user_id}
    query = (
        "select response->'chart'->>'view' from app.chat_turns"
        " where session_id = :sid and user_id = :uid"
    )
    if turn_id := last.get("chart_turn_id"):
        query += " and id = :tid"
        params["tid"] = turn_id
    else:
        # 턴 ID를 기록하기 전의 대화는 보기 설정을 뺀 차트로 원본 턴을 찾는다.
        params.update(turn_no=turn_no, chart=json.dumps(redact(last["chart"]), ensure_ascii=False))
        query += (
            " and turn_no <= :turn_no"
            " and (response->'chart') - 'view' = cast(:chart as jsonb) - 'view'"
            " order by turn_no desc, id desc limit 1"
        )
    async with engine.begin() as conn:
        return (await conn.execute(text(query), params)).scalar()


async def set_chart_view(user_id: int, session_id: str, turn_id: int, view: str) -> str:
    """화면에서 바꾼 차트 모양(꺾은선 · 막대 · 표)을 그 답에 남긴다 → "ok" · "missing" · "unfit".
    보고서는 이 답(chat_turns.response)을 복사해 만들어서, 남기지 않으면 말로 만든 보고서
    ('보고서 만들어줘' · 제안 칩 · '정리해줘')와 다시 연 대화가 처음 모양으로 돌아갔다"""
    from app.core.db import engine
    from app.services import charts

    async with engine.begin() as conn:
        row = await conn.execute(
            text(
                "select response from app.chat_turns"
                " where id = :tid and session_id = :sid and user_id = :uid for update"
            ),
            {"tid": turn_id, "sid": session_id, "uid": user_id},
        )
        response = row.scalar()
        chart = (response or {}).get("chart")
        if not isinstance(chart, dict):
            return "missing"
        if not charts.view_fits(chart, view):
            return "unfit"
        await conn.execute(
            text(
                "update app.chat_turns set response = jsonb_set(response, '{chart,view}',"
                " to_jsonb(cast(:view as text))) where id = :tid"
            ),
            {"view": view, "tid": turn_id},
        )
    return "ok"


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
