"""통합 보고서 초안: 만들기 · 담기 · 빼기 · 순서 · 목록 (명세서 4 · 6장)

담을 때 그 답(app.chat_turns 한 행)의 화면 내용을 report_items.snapshot에 복사한다.
chat_turns는 30일 뒤 지워지고 대화를 지우면 함께 지워지지만, 보고서는 snapshot으로 남는다.
보고서 생성(LLM)은 다음 단계에서 붙이고, 여기서는 항목이 바뀌면 생성 결과(content)를 비운다.
"""

import json
from typing import Any

from sqlalchemy import text

from app.services import charts
from app.services.log_redaction import mask

TITLE_MAX = 80
PURPOSE_MAX = 300
MAX_ITEMS = 8
DEFAULT_TITLE = "통계 분석 보고서"
SNAPSHOT_KEYS = ("status", "answer", "figure", "sources", "chart", "summary", "notice", "applied")


class ReportError(Exception):
    """API가 그대로 사용자에게 보여 줄 이유를 담는다"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


NOT_FOUND = "not_found"
NOT_REPORTABLE = "not_reportable"
FULL = "full"
BAD_ORDER = "bad_order"
CHANGED = "changed"
EMPTY = "empty"
OTHER_TALK = "other_conversation"
OTHER_TALK_MESSAGE = "보고서에는 한 대화의 답만 담을 수 있습니다. 이 답은 다른 대화에서 나왔습니다."


def clean_title(title: str | None) -> str:
    words = " ".join((mask(title or "") or "").split())
    return words[:TITLE_MAX] or DEFAULT_TITLE


def clean_purpose(purpose: str | None) -> str | None:
    words = " ".join((mask(purpose or "") or "").split())
    return words[:PURPOSE_MAX] or None


def snapshot_of(turn: dict) -> dict[str, Any]:
    response = turn.get("response") or {}
    snap = {k: response[k] for k in SNAPSHOT_KEYS if response.get(k) is not None}
    snap["question"] = turn.get("question")
    snap["turn_created_at"] = str(turn.get("created_at") or "")
    return snap


async def _report(conn, user_id: int, report_id: int, lock: bool = False) -> dict | None:
    row = await conn.execute(
        text(
            "select id, user_id, session_id, title, purpose, status, content, created_at,"
            " updated_at from app.reports where id = :rid and user_id = :uid"
            + (" for update" if lock else "")
        ),
        {"rid": report_id, "uid": user_id},
    )
    found = row.mappings().first()
    return dict(found) if found else None


async def _items(conn, report_id: int) -> list[dict]:
    rows = await conn.execute(
        text(
            "select turn_id, sort_order, snapshot, added_at from app.report_items"
            " where report_id = :rid order by sort_order, added_at"
        ),
        {"rid": report_id},
    )
    return [dict(r) for r in rows.mappings().all()]


async def _touch(conn, report_id: int) -> None:
    """항목이 바뀌면 이전 생성 결과는 더 이상 맞지 않는다"""
    await conn.execute(
        text(
            "update app.reports set updated_at = now(), content = null, status = 'draft'"
            " where id = :rid"
        ),
        {"rid": report_id},
    )


async def _turn(conn, user_id: int, turn_id: int) -> dict:
    row = await conn.execute(
        text(
            "select id, session_id, message as question, response, created_at from app.chat_turns"
            " where id = :tid and user_id = :uid"
        ),
        {"tid": turn_id, "uid": user_id},
    )
    turn = row.mappings().first()
    if turn is None:
        raise ReportError(NOT_FOUND, "그 답을 찾지 못했습니다. 30일이 지났거나 지운 대화입니다.")
    response = turn["response"] or {}
    if not response.get("reportable") or not response.get("figure"):
        raise ReportError(
            NOT_REPORTABLE,
            "검증된 수치로 답한 결과만 보고서에 담을 수 있습니다.",
        )
    return dict(turn)


async def _add(
    conn, user_id: int, report_id: int, turn_id: int, chart_view: str | None = None
) -> bool:
    """담았으면 True, 이미 있으면 False"""
    have = await conn.execute(
        text(
            "select count(*) as n, coalesce(max(sort_order), -1) as last,"
            " bool_or(turn_id = :tid) as dup from app.report_items where report_id = :rid"
        ),
        {"rid": report_id, "tid": turn_id},
    )
    n, last, dup = have.one()
    if dup:
        return False
    if n >= MAX_ITEMS:
        raise ReportError(FULL, f"보고서에는 결과를 {MAX_ITEMS}개까지 담을 수 있습니다.")
    turn = await _turn(conn, user_id, turn_id)
    await _same_talk(conn, report_id, turn["session_id"])
    snapshot = snapshot_of(turn)
    if chart_view and (chart := snapshot.get("chart")):
        kind = chart.get("type")
        if chart_view in ("table", kind) or (
            chart_view == "bar" and kind == "line" and not charts.crowded(chart)
        ):
            snapshot["chart"] = {**chart, "view": chart_view}
    await conn.execute(
        text(
            "insert into app.report_items (report_id, turn_id, sort_order, snapshot)"
            " values (:rid, :tid, :ord, cast(:snap as jsonb))"
        ),
        {
            "rid": report_id,
            "tid": turn_id,
            "ord": last + 1,
            "snap": json.dumps(snapshot, ensure_ascii=False, default=str),
        },
    )
    return True


async def _same_talk(conn, report_id: int, session_id: str) -> None:
    """보고서는 한 대화의 답으로만 만든다: 대화창의 [보고서]에서 그 대화의 답을 골라 만든다.
    보고서의 대화는 담긴 답에서 정해진다 (아직 없으면 이 답의 대화로)"""
    row = await conn.execute(
        text("select session_id from app.reports where id = :rid"), {"rid": report_id}
    )
    current = row.scalar()
    if current is None:
        await conn.execute(
            text("update app.reports set session_id = :sid where id = :rid"),
            {"sid": session_id, "rid": report_id},
        )
    elif current != session_id:
        raise ReportError(OTHER_TALK, OTHER_TALK_MESSAGE)


def _talk_of(turns: list[dict], asked: str | None) -> str | None:
    """고른 답들의 대화. 여러 대화가 섞였거나 화면이 보낸 대화와 다르면 거절한다"""
    talks = {t["session_id"] for t in turns}
    if len(talks) > 1 or (asked and talks and asked not in talks):
        raise ReportError(OTHER_TALK, "보고서에는 한 대화의 답만 담을 수 있습니다.")
    return next(iter(talks), None) or asked


async def _full(conn, user_id: int, report_id: int) -> dict:
    report = await _report(conn, user_id, report_id)
    if report is None:
        raise ReportError(NOT_FOUND, "보고서가 없습니다.")
    report.pop("user_id", None)
    report["items"] = await _items(conn, report_id)
    return report


async def create(
    user_id: int,
    title: str | None = None,
    purpose: str | None = None,
    turn_ids: list[int] | None = None,
    session_id: str | None = None,
    chart_views: dict[int, str] | None = None,
) -> dict:
    from app.core.db import engine

    async with engine.begin() as conn:
        # 보고서의 대화는 고른 답에서 정한다 (화면이 보낸 session_id는 맞는지만 본다)
        turns = [await _turn(conn, user_id, tid) for tid in dict.fromkeys(turn_ids or [])]
        row = await conn.execute(
            text(
                "insert into app.reports (user_id, session_id, title, purpose)"
                " values (:uid, :sid, :title, :purpose) returning id"
            ),
            {
                "uid": user_id,
                "sid": _talk_of(turns, session_id),
                "title": clean_title(title),
                "purpose": clean_purpose(purpose),
            },
        )
        report_id = row.scalar_one()
        for tid in dict.fromkeys(turn_ids or []):
            await _add(conn, user_id, report_id, tid, (chart_views or {}).get(tid))
        return await _full(conn, user_id, report_id)


async def listing(user_id: int, limit: int = 30, offset: int = 0) -> dict[str, Any]:
    from app.core.db import engine

    async with engine.begin() as conn:
        total = (
            await conn.execute(
                text("select count(*) from app.reports where user_id = :uid"), {"uid": user_id}
            )
        ).scalar()
        rows = await conn.execute(
            text(
                "select r.id, r.title, r.status, r.created_at, r.updated_at,"
                " (select count(*) from app.report_items i where i.report_id = r.id) as items"
                " from app.reports r where r.user_id = :uid"
                " order by r.updated_at desc limit :limit offset :offset"
            ),
            {"uid": user_id, "limit": limit, "offset": offset},
        )
        return {"total": total, "reports": [dict(r) for r in rows.mappings().all()]}


async def detail(user_id: int, report_id: int) -> dict | None:
    from app.core.db import engine

    async with engine.begin() as conn:
        try:
            return await _full(conn, user_id, report_id)
        except ReportError:
            return None


async def latest_for_conversation(user_id: int, session_id: str) -> dict | None:
    from app.core.db import engine

    async with engine.begin() as conn:
        report_id = await conn.scalar(
            text(
                "select id from app.reports where user_id = :uid and session_id = :sid"
                " order by updated_at desc, id desc limit 1"
            ),
            {"uid": user_id, "sid": session_id},
        )
        return await _full(conn, user_id, report_id) if report_id is not None else None


async def update(
    user_id: int,
    report_id: int,
    title: str | None = None,
    purpose: str | None = None,
    order: list[int] | None = None,
) -> dict:
    """제목 · 목적 · 항목 순서. 순서는 지금 담긴 항목 전부를 한 번씩 적어야 한다"""
    from app.core.db import engine

    async with engine.begin() as conn:
        if await _report(conn, user_id, report_id, lock=True) is None:
            raise ReportError(NOT_FOUND, "보고서가 없습니다.")
        sets, params = [], {"rid": report_id}
        if title is not None:
            sets.append("title = :title")
            params["title"] = clean_title(title)
        if purpose is not None:
            sets.append("purpose = :purpose")
            params["purpose"] = clean_purpose(purpose)
        if sets:
            await conn.execute(
                text(
                    f"update app.reports set {', '.join(sets)}, updated_at = now() where id = :rid"
                ),
                params,
            )
        if order is not None:
            have = [i["turn_id"] for i in await _items(conn, report_id)]
            if sorted(order) != sorted(have) or len(set(order)) != len(order):
                raise ReportError(BAD_ORDER, "순서에는 담긴 항목을 모두 한 번씩 적어 주세요.")
            for i, tid in enumerate(order):
                await conn.execute(
                    text(
                        "update app.report_items set sort_order = :ord"
                        " where report_id = :rid and turn_id = :tid"
                    ),
                    {"rid": report_id, "tid": tid, "ord": i},
                )
            await _touch(conn, report_id)
        return await _full(conn, user_id, report_id)


async def add_item(user_id: int, report_id: int, turn_id: int) -> dict:
    """같은 항목을 다시 담아도 그대로 돌려준다 (버튼을 두 번 눌러도 안전)"""
    from app.core.db import engine

    async with engine.begin() as conn:
        if await _report(conn, user_id, report_id, lock=True) is None:
            raise ReportError(NOT_FOUND, "보고서가 없습니다.")
        if await _add(conn, user_id, report_id, turn_id):
            await _touch(conn, report_id)
        return await _full(conn, user_id, report_id)


async def add_to_draft(user_id: int, turn_id: int, session_id: str | None = None) -> dict:
    """[보고서에 담기] 버튼: 그 답이 나온 대화의 가장 최근 초안에 담는다. 초안이 없으면 만든다.
    보고서는 한 대화의 답으로만 만든다: 다른 대화의 초안에 섞지 않는다"""
    from app.core.db import engine

    async with engine.begin() as conn:
        # 초안이 없을 때 두 요청이 동시에 오면 둘 다 새 초안을 만든다.
        # 사용자 행을 잠가 같은 사용자의 담기를 하나씩 처리한다
        await conn.execute(
            text("select id from app.users where id = :uid for update"), {"uid": user_id}
        )
        # 담지 못할 답이면 빈 초안을 만들지 않는다. 대화는 답에서 정한다
        talk = _talk_of([await _turn(conn, user_id, turn_id)], session_id)
        row = await conn.execute(
            text(
                "select id from app.reports where user_id = :uid and session_id = :sid"
                " and status = 'draft' and content is null"
                " order by updated_at desc limit 1 for update"
            ),
            {"uid": user_id, "sid": talk},
        )
        report_id = row.scalar()
        if report_id is None:
            row = await conn.execute(
                text(
                    "insert into app.reports (user_id, session_id, title)"
                    " values (:uid, :sid, :title) returning id"
                ),
                {"uid": user_id, "sid": talk, "title": DEFAULT_TITLE},
            )
            report_id = row.scalar_one()
        if await _add(conn, user_id, report_id, turn_id):
            await _touch(conn, report_id)
        return await _full(conn, user_id, report_id)


async def remove_item(user_id: int, report_id: int, turn_id: int) -> dict:
    from app.core.db import engine

    async with engine.begin() as conn:
        if await _report(conn, user_id, report_id, lock=True) is None:
            raise ReportError(NOT_FOUND, "보고서가 없습니다.")
        gone = await conn.execute(
            text("delete from app.report_items where report_id = :rid and turn_id = :tid"),
            {"rid": report_id, "tid": turn_id},
        )
        if gone.rowcount:
            await _touch(conn, report_id)
        return await _full(conn, user_id, report_id)


async def delete(user_id: int, report_id: int) -> bool:
    from app.core.db import engine

    async with engine.begin() as conn:
        gone = await conn.execute(
            text("delete from app.reports where id = :rid and user_id = :uid"),
            {"rid": report_id, "uid": user_id},
        )
    return gone.rowcount > 0


async def generate(user_id: int, report_id: int, style: str | None = None) -> dict:
    """담긴 결과로 보고서 문장을 만들어 content에 저장한다. 생성 중 항목이 바뀌면 저장하지 않는다"""
    from app.core.db import engine
    from app.services import report_writer

    async with engine.begin() as conn:
        report = await _full(conn, user_id, report_id)
    if not report["items"]:
        raise ReportError(EMPTY, "보고서에 담긴 결과가 없습니다.")
    seen = [(i["turn_id"], i["sort_order"]) for i in report["items"]]
    stamp = report["updated_at"]
    try:
        content = await report_writer.write(
            report["title"], report["purpose"], report["items"], style
        )
        status = "ready"
    except ValueError as e:
        content, status = {"error": str(e)}, "failed"
    async with engine.begin() as conn:
        if await _report(conn, user_id, report_id, lock=True) is None:
            raise ReportError(NOT_FOUND, "보고서가 없습니다.")
        now = [(i["turn_id"], i["sort_order"]) for i in await _items(conn, report_id)]
        latest = await _report(conn, user_id, report_id)
        # 항목 · 순서 · 제목 · 목적 중 하나라도 바뀌었으면 이전 내용으로 만든 것이라 저장하지 않는다
        if now != seen or latest is None or latest["updated_at"] != stamp:
            raise ReportError(
                CHANGED, "만드는 동안 보고서 내용이 바뀌었습니다. 다시 만들어 주세요."
            )
        await conn.execute(
            text(
                "update app.reports set content = cast(:content as jsonb), status = :status,"
                " updated_at = now() where id = :rid"
            ),
            {
                "rid": report_id,
                "status": status,
                "content": json.dumps(content, ensure_ascii=False, default=str),
            },
        )
        return await _full(conn, user_id, report_id)
