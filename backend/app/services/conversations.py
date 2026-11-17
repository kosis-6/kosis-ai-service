"""로그인 유저의 대화 기록 보관 (app.conversations, 마이그레이션 0019)

로그인 유저가 나눈 대화를 계정에 남겨, 목록에서 다시 열어 이어 가거나 새 창으로 새로 시작한다.

  대화 목록   app.conversations (주인, 제목 = 첫 질문, 마지막 대화 시각)
  턴          app.chat_turns (질문 + 화면에 보여 준 답 전체 response)
  이어 갈 조건 app.chat_sessions "ask:{id}" (ask_session.load/save, 서버 메모리에 사본)

- 다른 계정의 대화는 목록에도 없고, id를 알아도 열리지 않는다 (모든 조회에 user_id 조건)
- 창마다 따로 기억한다. 새 창(새 session_id)은 빈 대화로 시작한다
- 손님 대화는 남기지 않는다. 마지막 대화 뒤 log_retention.KEEP_DAYS(30일)가 지나면 지운다
- 저장하는 질문·답은 기존 턴 기록과 같이 마스킹한다 (log_redaction)
"""

from typing import Any

from sqlalchemy import text

from app.services.log_redaction import mask

TITLE_MAX = 60
# 예전 대화를 열 때 다시 보여 줄 응답 칸 (/ask 응답과 같은 이름)
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
    """화면에 보여 준 답에서 다시 보여 줄 칸만. 되묻기의 내부 정보(context)는 뺀다"""
    out = {k: res.get(k) for k in REPLAY_KEYS if res.get(k) is not None}
    if isinstance(out.get("clarification"), dict):
        out["clarification"] = {k: v for k, v in out["clarification"].items() if k != "context"}
    return out


async def touch(session_id: str, user_id: int, question: str) -> None:
    """대화가 이어졌다. 처음이면 첫 질문을 제목으로 만든다 (다른 계정의 대화는 건드리지 않는다)"""
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
    """내 대화 목록 (최근 것부터)"""
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
    """한 대화의 턴 (질문 + 보여 준 답). 내 대화가 아니면 None"""
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
        # 기록을 남기기 전(0019 이전)의 턴은 답 첫 부분만 있다
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
    """대화를 지운다: 목록, 턴, 이어 갈 조건, 서버 메모리 사본. 내 대화가 아니면 False"""
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
