"""보고서 에이전트 (명세서 7장): 보고서에 관한 말을 받아 도구를 골라 가며 처리한다

라우터(report_router, 코드)가 보고서 말만 여기로 보낸다. 통계 질문은 기존 /ask 그대로.
한 번에 행동 하나를 JSON으로 받는다 (함수 호출 기능에 기대지 않아 HCX · Gemma 둘 다 된다).
숫자 조회 · 계산 · 검사는 도구(report_tools) 안의 코드가 하고, 에이전트는 고르기만 한다.

상한: 행동 MAX_STEPS 번 · ask_stat 3번 · 전체 TIMEOUT 초. 넘으면 지금까지 한 것을 저장하고 알린다
잘못된 출력(JSON 아님 · 없는 도구)은 그 사실을 알려 다시 고르게 하고, 두 번 연속이면 되묻는다
"""

import asyncio
import inspect
import json
import logging
import re
import time
from typing import Any

from app.agent import report_tools as tools
from app.agent.report_router import agent_state
from app.core.config import get_settings
from app.prompts import report_agent as prompt
from app.services import llm_calls, llm_json, search_llm
from app.services.hcx_client import HcxClient

logger = logging.getLogger(__name__)

MODEL = "HCX-007"
MAX_STEPS = 8
TIMEOUT = 60.0
MAX_TOKENS = 600
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
ACTION = re.compile(r'"action"\s*:\s*"(\w+)"')
COUNT = re.compile(r"\d{1,2}\s*(개|번째|가지)")  # 담은 개수 · 순서는 통계 값이 아니다
DONE_FALLBACK = "요청한 내용을 처리했어요. 내 보고서에서 확인해 주세요."
# 이 행동이 성공하면 그 요청은 끝낸다 (HCX 가 맞게 담은 뒤에도 다른 행동을 이어 가며 결과를 망쳤다,
# 개발셋 R15). 보고서를 만들어 달라고 했거나 정정의 빼기 뒤에는 이어 간다
SMALL_EDITS = {"add_item", "remove_item", "reorder", "set_title"}


@llm_calls.staged("report_agent")
async def _ask(messages: list[dict]) -> dict | None:
    client = (
        search_llm.GemmaClient(retry_waits=())
        if get_settings().report_llm == "gemma"
        else HcxClient()
    )
    result = await client.chat(
        MODEL,
        messages,
        thinking={"effort": "none"},
        maxCompletionTokens=MAX_TOKENS,
        temperature=0,
    )
    return read_action(result.content)


def read_action(content: str | None) -> dict | None:
    """행동 JSON 읽기. HCX 가 행동 둘을 한 번에 쓰며 args 를 도구 이름별로 넣고 괄호를 더 닫아
    action 앞에서 JSON 이 끝나는 일이 있었다 (개발셋 R15): 원문에서 action 을 찾고, args 안에
    그 행동 이름의 인자가 있으면 그것을 쓴다"""
    out = llm_json.loads(content)[0]
    if out is None:
        return None
    if not out.get("action") and (m := ACTION.search(content or "")):
        out["action"] = m[1]
    args = out.get("args")
    if isinstance(args, dict) and isinstance(args.get(out.get("action")), dict):
        out["args"] = args[out["action"]]
    return out


def _done_after(action: str, ctx: tools.Ctx) -> bool:
    if tools.ASKS_WRITE.search(ctx.question):
        return False
    return not (action == "remove_item" and tools.CORRECTION.search(ctx.question))


def _report_line(ctx: tools.Ctx) -> str:
    """지금 보고서에 담긴 것 (코드가 쓰는 말: 결과 이름만, 값은 쓰지 않는다)"""
    items = (ctx.report or {}).get("items") or []
    names = [tools._label(i["snapshot"]) or i["snapshot"].get("question") or "" for i in items]
    if not names:
        return "보고서가 비었어요."
    return f"내 보고서에 {len(names)}개가 있어요: " + ", ".join(names)


def _safe_message(message: str, seen: str, fallback: str) -> str:
    """에이전트가 쓴 말의 숫자는 도구 결과 · 사용자 말에 있던 것만 허용한다"""
    message = " ".join(str(message or "").split())
    allowed = set(NUMBER.findall(seen))
    if not message or any(n not in allowed for n in NUMBER.findall(COUNT.sub("", message))):
        return fallback
    return message


async def _context(ctx: tools.Ctx) -> dict[str, Any]:
    return {
        "user_message": ctx.question,
        "pending": ctx.saved.get("pending"),
        # 바로 앞 답에서 "보고서를 만들까요?"를 제안했으면 그 주제
        "offer": ctx.saved.get("offer_topic")
        if ctx.saved.get("offer_turn") == ctx.turn_no
        else None,
        # 앞 요청에서 조회 · 찾기 뒤 "담을까요?"라고 물은 답들. 사용자가 확인하면 바로 add_item
        "waiting_for_confirm": await tools.describe(ctx, sorted(ctx.confirmable)),
        "ask_stat_left": tools.MAX_ASK_STAT,
        "turns": (turns := await tools.session_turns(ctx)),
        # "그거" · "이거"가 가리키는 바로 앞 답
        "last_answer_turn_id": turns[-1]["turn_id"] if turns else None,
        "report": tools.report_view(await tools.current_report(ctx)),
    }


async def _loop(ctx: tools.Ctx, trace: list[dict]) -> dict[str, Any]:
    context = await _context(ctx)
    messages = [
        {"role": "system", "content": prompt.SYSTEM},
        {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)},
    ]
    seen = json.dumps(context, ensure_ascii=False, default=str)
    invalid = 0
    failed: str | None = None  # 바로 앞에 실패한 행동
    for _ in range(MAX_STEPS):
        out = await _ask(messages)
        action = (out or {}).get("action")
        args = (out or {}).get("args") or {}
        if not isinstance(args, dict):
            args = {}
        if action not in tools.TOOLS and action not in tools.ENDINGS:
            invalid += 1
            trace.append({"action": action, "error": "invalid"})
            if invalid >= 2:
                return {
                    "end": "ask_user",
                    "question": "보고서에서 무엇을 할까요?",
                    "options": [],
                    "seen": seen,
                }
            messages += [
                {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
                {"role": "user", "content": "JSON 하나와 도구 목록에 있는 action 만 쓰세요."},
            ]
            continue
        invalid = 0
        if action in tools.ENDINGS:
            trace.append({"action": action, "args": args})
            return {"end": action, **args, "seen": seen}
        tool = tools.TOOLS[action]
        params = inspect.signature(tool).parameters
        if not any(p.kind is p.VAR_KEYWORD for p in params.values()):
            # HCX 가 args 에 "action" · "reason" 같은 것을 섞어 넣는다: 도구가 받지 않는 것은 버린다
            args = {k: v for k, v in args.items() if k in params}
        try:
            result = await tool(ctx, **args)
        except TypeError as e:  # 도구에 없는 인자
            result = {"error": f"args 가 맞지 않습니다: {e}"}
        trace.append({"action": action, "args": args, "result": result})
        error = isinstance(result, dict) and result.get("error") and not result.get("added")
        if action in SMALL_EDITS and not error and _done_after(action, ctx):
            return {"end": "finish", "message": _report_line(ctx), "seen": seen + _report_line(ctx)}
        if (
            error and failed == action
        ):  # 같은 행동이 두 번 연속 막히면 그만 (같은 실패를 되풀이하지 않게)
            return {"end": "finish", "message": "", "seen": seen}
        failed = action if error else None
        observation = json.dumps(result, ensure_ascii=False, default=str)
        seen += observation
        messages += [
            {"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
            {"role": "user", "content": f"도구 결과: {observation}"},
        ]
    return {"end": "finish", "message": "", "seen": seen, "limit": "steps"}


def _reply(ctx: tools.Ctx, end: dict) -> tuple[str, dict | None]:
    kind = end.get("end")
    seen = end.get("seen") or ""
    if kind == "refuse":
        why = tools.REFUSE_WHY.get(end.get("why") or "", "")
        return _safe_message(end.get("message") or why, seen, why or DONE_FALLBACK), None
    if kind == "ask_user":
        options = [str(o)[:60] for o in end.get("options") or [] if str(o).strip()][:4]
        question = _safe_message(end.get("question"), seen, "어떻게 할까요?")
        clar = {"kind": "report_choice", "prompt": question, "options": options}
        return question, clar
    return _safe_message(end.get("message"), seen, DONE_FALLBACK), None


async def run(question: str, sid: str, state: dict | None, user_id: int | None) -> dict[str, Any]:
    """보고서 경로의 응답 (AskResponse 모양) + res["report_agent_state"] (다음 요청에 넘길 것)"""
    saved = agent_state(state)
    base = {
        "status": "report",
        "request_type": "report",
        "parsed": {},
        "result": {},
        "sources": [],
        "question": question,
    }
    if user_id is None:
        return {**base, "answer": tools.REFUSE_WHY["login"], "report_agent_state": {}}
    ctx = tools.Ctx(sid, user_id, question, saved)
    ctx.turn_no = (state or {}).get("turn_no")
    trace: list[dict] = []
    started = time.monotonic()
    try:
        end = await asyncio.wait_for(_loop(ctx, trace), timeout=TIMEOUT)
    except TimeoutError:
        end = {"end": "finish", "message": "", "limit": "time"}
    except Exception as e:
        logger.exception("보고서 에이전트 실패")
        end = {"end": "finish", "message": "", "error": str(e)}
    answer, clar = _reply(ctx, end)
    if end.get("limit"):
        answer = "처리할 일이 많아 여기까지 했어요. 내 보고서에서 확인하고 이어서 말씀해 주세요."
    elif end.get("error"):
        answer = "보고서를 처리하지 못했어요. 잠시 후 다시 말씀해 주세요."
    pending = None
    candidates: list[int] = []
    if end.get("end") == "ask_user":
        pending = {"question": answer, "options": (clar or {}).get("options") or []}
        candidates = sorted(set(ctx.new_candidates) | ctx.confirmable)
    report = ctx.report
    res = {
        **base,
        "answer": answer,
        "clarification": clar,
        "report": report,
        "trace": {
            "report_agent": {
                "steps": trace,
                "end": end.get("end"),
                "limit": end.get("limit"),
                "ms": round((time.monotonic() - started) * 1000),
            }
        },
    }
    res["report_agent_state"] = {
        "pending": pending,
        "candidates": candidates,
        "declined": bool(saved.get("declined") or ctx.declined),
        "report_id": (report or {}).get("id") or saved.get("report_id"),
        "last_added": ctx.added_now,
    }
    return res
