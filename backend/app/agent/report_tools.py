"""보고서 에이전트의 도구 (명세서 7.3)

에이전트(LLM)는 다음 행동만 고르고, 검사는 모두 여기 코드가 한다.
  - 담을 수 있는 답(reportable) · 본인 답만 담긴다 (reports 서비스가 다시 확인)
  - 이번 요청에서 새로 조회했거나(ask_stat) 다른 대화에서 찾은(find_turns all) 답은
    사용자가 확인하기 전에는 담지 못한다: ask_user 로 물은 뒤 다음 요청에서만 담긴다
  - ask_stat 은 한 요청에 MAX_ASK_STAT 번까지
"""

import re
from typing import Any

from sqlalchemy import text

from app.services import report_compare, reports
from app.services.reports import ReportError

MAX_ASK_STAT = 3
# 사용자 말에 이런 요청이 있어야 그 도구가 움직인다
# (LLM이 시키지 않은 일을 하지 않게, 개발셋 R03 · R04)
ASKS_WRITE = re.compile(r"만들|써|쓰|작성|정리|생성|보고서로")
# "아니 서울 거" · "부산 말고 서울"은 바꿔 담으라는 말 (개발셋 R15)
ASKS_ADD = re.compile(r"담|넣|추가|포함|만들|써|작성|정리|보고서로|^\s*아니|말고")
# "그거 · 이거"는 바로 앞 답 하나 (개발셋 R15). "다 · 모두 · 전부"가 있으면 여럿
ONE_POINTED = re.compile(r"(그거|이거|그것|이것|저거)(?!들)")
# 바로 앞에 담은 것을 고치는 말: 새로 담으면 앞에서 담은 것은 뺀다 (바꿔 담기, 개발셋 R15-T4)
CORRECTION = re.compile(r"^\s*아니|말고")
MANY = re.compile(r"다\s|다$|모두|전부|둘|두\s*개|세\s*개")
STOP = {
    "보고서",
    "보고서에",
    "넣어줘",
    "넣어",
    "담아",
    "담아줘",
    "어제",
    "아까",
    "본",
    "것",
    "거",
    "그거",
    "좀",
    "그",
}
WORD = re.compile(r"[가-힣A-Za-z0-9]{2,}")
REFUSE_WHY = {
    "out_of_scope": "제공 범위(KOSIS 인구 통계) 밖이라 보고서에 넣을 수 없어요.",
    "unverified": "검증된 수치로 답한 결과만 보고서에 담을 수 있어요.",
    "numbers": "보고서의 숫자는 원자료 값 그대로만 쓸 수 있어요.",
    "causal": "통계 값이 달라진 원인은 판단하지 않아요. 관련 통계를 더 담아 비교해 볼 수 있어요.",
    "login": "보고서는 로그인한 뒤에 만들 수 있어요.",
    "other_talk": "보고서는 한 대화의 답으로 만들어요. 그 대화에서 [보고서]를 눌러 주세요.",
}


class Ctx:
    """한 요청 동안 도구들이 함께 쓰는 상태"""

    def __init__(self, sid: str, user_id: int, question: str, saved: dict):
        self.sid = sid
        self.user_id = user_id
        self.question = question
        self.saved = saved  # 앞 요청에서 넘어온 report_agent 상태
        self.confirmable: set[int] = set(saved.get("candidates") or [])
        self.session_ids: set[int] = set()
        self.new_candidates: list[int] = []  # 이번 요청에서 처음 본, 확인이 필요한 답
        self.ask_stat_calls = 0
        self.report: dict | None = None
        self.declined = False
        self.turn_no: int | None = None
        self.added_now: list[int] = []


def _label(response: dict) -> str | None:
    return ((response or {}).get("figure") or {}).get("label")


def _value(response: dict) -> str | None:
    fig = (response or {}).get("figure")
    if not fig or fig.get("value") is None:
        return None
    return report_compare.value_text({"figure": fig})


def _turn_row(r: dict) -> dict:
    resp = r["response"] or {}
    return {
        "turn_id": r["id"],
        "question": r["question"],
        "label": _label(resp),
        "value": _value(resp),
        "reportable": bool(resp.get("reportable")),
    }


async def session_turns(ctx: Ctx, limit: int = 20) -> list[dict]:
    """이 대화의 답들 (최근 limit 개). 에이전트에게 보여 줄 목록"""
    from app.core.db import engine

    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "select id, message as question, response from app.chat_turns"
                " where session_id = :sid and user_id = :uid"
                " and coalesce(request_type, '') <> 'report'"
                " order by turn_no desc, id desc limit :limit"
            ),
            {"sid": ctx.sid, "uid": ctx.user_id, "limit": limit},
        )
        out = [_turn_row(dict(r)) for r in rows.mappings().all()][::-1]
    ctx.session_ids = {t["turn_id"] for t in out}
    return out


async def describe(ctx: Ctx, turn_ids: list[int]) -> list[dict]:
    """확인을 기다리는 답들의 이름 · 값 (에이전트가 무엇을 담을지 알도록)"""
    if not turn_ids:
        return []
    from app.core.db import engine

    async with engine.begin() as conn:
        rows = await conn.execute(
            text(
                "select id, message as question, response from app.chat_turns"
                " where user_id = :uid and id = any(:ids)"
            ),
            {"uid": ctx.user_id, "ids": list(turn_ids)},
        )
        return [_turn_row(dict(r)) for r in rows.mappings().all()]


async def current_report(ctx: Ctx) -> dict | None:
    if ctx.report is not None:
        return ctx.report
    found = await reports.listing(ctx.user_id, limit=1)
    if found["reports"]:
        ctx.report = await reports.detail(ctx.user_id, found["reports"][0]["id"])
    return ctx.report


def report_view(report: dict | None) -> dict | None:
    """에이전트에게 보여 줄 보고서 요약 (snapshot 전체는 주지 않는다)"""
    if not report:
        return None
    return {
        "id": report["id"],
        "title": report["title"],
        "status": report["status"],
        "items": [
            {
                "turn_id": i["turn_id"],
                "label": _label(i["snapshot"]) or i["snapshot"].get("question"),
                "value": _value(i["snapshot"]),
            }
            for i in report["items"]
        ],
    }


# ---- 도구 ---------------------------------------------------------------------------


async def find_turns(ctx: Ctx, text_: str = "", **args: Any) -> dict:
    """이 대화의 답에서 찾기. 보고서는 한 대화의 답으로만 만든다 (#253): 다른 대화는 찾지 않는다"""
    # 프롬프트는 {"text": ...}로 보내라고 한다 (text 는 이 모듈의 SQL 함수 이름이라 text_ 로 받는다)
    text_ = str(args.get("text") or text_ or "")
    words = [w for w in WORD.findall(text_) if w not in STOP]
    turns = await session_turns(ctx)
    hits = [
        t
        for t in turns
        if not words or any(w in (t["label"] or t["question"] or "") for w in words)
    ]
    out: dict[str, Any] = {"candidates": hits[-5:]}
    if not hits:
        out["note"] = (
            "이 대화에서 찾지 못했습니다. 다른 대화의 답은 담을 수 없습니다 (refuse other_talk)"
        )
    return out


async def ask_stat(ctx: Ctx, question: str = "") -> dict:
    if ctx.ask_stat_calls >= MAX_ASK_STAT:
        return {
            "error": f"ask_stat 은 한 요청에 {MAX_ASK_STAT}번까지입니다."
            " 지금까지 결과로 ask_user 하세요",
            "ask_stat_left": 0,
        }
    if not question.strip():
        return {"error": "question 이 비었습니다"}
    ctx.ask_stat_calls += 1
    from app.services import ask_session

    res = await ask_session.handle(None, question, ctx.user_id, listed=False)
    tid = res.get("turn_id")
    if tid:
        await _join_talk(ctx, tid)
    out = {
        "turn_id": tid,
        "status": res.get("status"),
        "label": _label(res),
        "value": _value(res),
        "reportable": bool(res.get("reportable")),
        "answer": (res.get("answer") or "").split("\n")[0][:200],
    }
    if tid and out["reportable"]:
        ctx.new_candidates.append(tid)
    out["ask_stat_left"] = MAX_ASK_STAT - ctx.ask_stat_calls
    return out


async def _join_talk(ctx: Ctx, turn_id: int) -> None:
    """따로 조회한 답을 이 대화의 답으로 옮긴다. 보고서는 한 대화의 답만 담으므로 (#253)
    옮기지 않으면 담을 수 없다. 대화를 다시 열면 이 질문 · 답도 대화 안에 보인다"""
    from app.core.db import engine

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "update app.chat_turns set session_id = :sid, turn_no = :no"
                " where id = :tid and user_id = :uid"
            ),
            {"sid": ctx.sid, "no": ctx.turn_no or 0, "tid": turn_id, "uid": ctx.user_id},
        )


async def add_item(ctx: Ctx, turn_ids: list[int] | None = None) -> dict:
    if not ASKS_ADD.search(ctx.question) and not ctx.confirmable:
        return {"error": "사용자가 담아 달라고 하지 않았습니다. 요청한 일만 하고 finish 하세요"}
    turns = await session_turns(ctx)
    done, problems = [], []
    turn_ids = list(turn_ids or [])
    if ONE_POINTED.search(ctx.question) and not MANY.search(ctx.question) and turns:
        last = turns[-1]["turn_id"]
        if len(turn_ids) > 1 or (turn_ids and turn_ids[0] != last and not ctx.confirmable):
            problems.append(f"'그거 · 이거'는 바로 앞 답({last}) 하나입니다")
            turn_ids = [last] if last in turn_ids else []
    replaced = []
    last_added = ctx.saved.get("last_added") or []
    if CORRECTION.search(ctx.question) and turn_ids and last_added:
        # "아니 ~"는 앞에서 담은 것이 틀렸다는 말: LLM 이 다시 골라도
        # 앞에서 담은 것은 빼고 바꿔 담는다
        # (HCX 가 "아니 서울 거"를 "서울도"로 읽어 둘 다 담았다, 개발셋 R15-T4)
        turn_ids = [t for t in turn_ids if t not in last_added]
        report = await current_report(ctx)
        for old in last_added:
            if report and old in [i["turn_id"] for i in report["items"]]:
                report = await reports.remove_item(ctx.user_id, report["id"], old)
                replaced.append(old)
        ctx.report = report
    for tid in turn_ids:
        if tid in ctx.new_candidates or (tid not in ctx.session_ids and tid not in ctx.confirmable):
            problems.append(
                f"{tid}: 사용자 확인 전에는 담을 수 없습니다 (ask_user 로 먼저 물으세요)"
            )
            continue
        try:
            ctx.report = await reports.add_to_draft(ctx.user_id, tid, ctx.sid)
            done.append(tid)
            ctx.added_now.append(tid)
        except ReportError as e:
            problems.append(f"{tid}: {e.message}")
    out: dict[str, Any] = {"added": done, "report": report_view(ctx.report)}
    if replaced:
        out["removed_as_correction"] = replaced
    if problems:
        out["error"] = problems
    return out


async def remove_item(ctx: Ctx, turn_ids: list[int] | None = None) -> dict:
    report = await current_report(ctx)
    if not report:
        return {"error": "보고서가 없습니다"}
    for tid in turn_ids or []:
        report = await reports.remove_item(ctx.user_id, report["id"], tid)
    ctx.report = report
    return {"report": report_view(report)}


async def reorder(ctx: Ctx, turn_ids: list[int] | None = None) -> dict:
    report = await current_report(ctx)
    if not report:
        return {"error": "보고서가 없습니다"}
    try:
        ctx.report = await reports.update(ctx.user_id, report["id"], order=list(turn_ids or []))
    except ReportError as e:
        return {"error": e.message}
    return {"report": report_view(ctx.report)}


async def set_title(ctx: Ctx, title: str | None = None, purpose: str | None = None) -> dict:
    report = await current_report(ctx)
    if not report:
        return {"error": "보고서가 없습니다"}
    ctx.report = await reports.update(ctx.user_id, report["id"], title=title, purpose=purpose)
    return {"report": report_view(ctx.report)}


async def check_compare(ctx: Ctx) -> dict:
    report = await current_report(ctx)
    if not report or not report["items"]:
        return {"error": "담긴 결과가 없습니다"}
    items = report_compare.items_of(report["items"])
    cmp = report_compare.compare(items)
    ids = {it["id"]: it["turn_id"] for it in items}
    return {
        "not_comparable": [
            {"turn_ids": [ids[p] for p in x["pair"]], "why": x["why"]}
            for x in cmp["not_comparable"]
        ],
        "trend_ok": cmp["trend_ok"],
    }


async def write_report(
    ctx: Ctx, title: str | None = None, purpose: str | None = None, style: str | None = None
) -> dict:
    if not ASKS_WRITE.search(ctx.question) and not ctx.saved.get("offer_topic"):
        return {
            "error": "사용자가 보고서를 만들어 달라고 하지 않았습니다."
            " 요청한 일만 하고 finish 하세요"
        }
    report = await current_report(ctx)
    if not report or not report["items"]:
        return {"error": "담긴 결과가 없습니다. 먼저 add_item 으로 담으세요"}
    if title or purpose:
        report = await reports.update(ctx.user_id, report["id"], title=title, purpose=purpose)
    try:
        ctx.report = await reports.generate(ctx.user_id, report["id"], style)
    except ReportError as e:
        return {"error": e.message}
    content = ctx.report.get("content") or {}
    return {
        "status": ctx.report["status"],
        "summary": [s["text"] for s in content.get("summary") or []][:3],
        "limits": content.get("limits") or [],
    }


async def revise_text(ctx: Ctx, style: str = "") -> dict:
    report = await current_report(ctx)
    if not report or report["status"] != "ready":
        return {"error": "아직 만든 보고서가 없습니다. write_report 를 먼저 부르세요"}
    return await write_report(ctx, style=style or None)


async def decline_offer(ctx: Ctx) -> dict:
    ctx.declined = True
    return {"ok": True}


TOOLS = {
    "find_turns": find_turns,
    "ask_stat": ask_stat,
    "add_item": add_item,
    "remove_item": remove_item,
    "reorder": reorder,
    "set_title": set_title,
    "check_compare": check_compare,
    "write_report": write_report,
    "revise_text": revise_text,
    "decline_offer": decline_offer,
}
# 이 행동을 고르면 이번 요청이 끝난다 (사용자에게 말을 돌려준다)
ENDINGS = {"finish", "ask_user", "refuse"}
