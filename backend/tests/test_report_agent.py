"""보고서 에이전트: 라우터 · 도구의 안전장치 · 고리 상한 · 말 검사 (명세서 7장)

LLM은 정해 둔 행동을 차례로 돌려주는 흉내로 바꾸고, DB는 실제 app 스키마를 쓴다 (CI에서도 돈다).
"""

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text

from app.agent import report_agent as ra
from app.agent import report_router as router
from app.agent import report_tools as tools
from app.services import reports

FIG = {
    "label": "2020년 서울특별시 1인가구",
    "indicator": "1인가구",
    "value": 1390701,
    "unit": "가구",
    "kind": "value",
    "conditions": {"region": "서울특별시", "period": "2020년"},
    "is_projection": False,
    "tbl_ids": ["DT_A"],
    "rows": [],
    "operands": [],
}


def run(coro):
    async def go():
        from app.core.db import engine

        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(go())


# ---- 라우터 ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("q", "state", "expected"),
    [
        ("이거 보고서에 담아줘", None, True),
        ("그거 넣어줘", None, True),
        ("2015년 것도 넣어줘", None, True),
        ("이것도 담아", None, True),
        ("서울도 넣어서 비교해줘", None, False),
        ("서울 빼고 나머지 시도 순위", None, False),
        ("2015년 것도 같이 보여줘", None, False),
        ("부산은?", {"turn_no": 2, "report_agent": {"last_turn": 2}}, False),
        ("두 번째 거 빼", {"turn_no": 2, "report_agent": {"last_turn": 2}}, True),
        ("두 번째 거 빼", {"turn_no": 3, "report_agent": {"last_turn": 2}}, False),
        ("응 넣어", {"turn_no": 3, "report_agent": {"pending": {"question": "담을까요?"}}}, True),
        ("응 만들어줘", {"turn_no": 3, "report_agent": {"offer_turn": 3}}, True),
        ("응 만들어줘", {"turn_no": 4, "report_agent": {"offer_turn": 3}}, False),
    ],
)
def test_router(q, state, expected):
    assert router.wants(q, state) is expected


def test_standalone_stat_exits_report_confirmation():
    state = {"turn_no": 3, "report_agent": {"pending": {"question": "어떤 주제인가요?"}}}
    assert not router.wants("2024년 부산 출생아 수 알려줘", state, standalone_stat=True)
    assert router.wants(
        "2024년 부산 출생아 수 알려주고 보고서에 담아줘", state, standalone_stat=True
    )
    assert router.wants("부산 출생아 수", state)


# ---- DB 준비 ------------------------------------------------------------------------------


async def _setup():
    from app.core.db import engine

    tag = uuid.uuid4().hex[:10]
    sid = f"agent-test-{tag}"
    async with engine.begin() as conn:
        uid = (
            await conn.execute(
                text("insert into app.users (email, password_hash) values (:e, 'x') returning id"),
                {"e": f"agent-test-{tag}@example.com"},
            )
        ).scalar_one()
        turns = []
        for i, (q, resp) in enumerate(
            [
                ("2020년 서울 1인 가구 수는?", {"status": "ok", "figure": FIG, "reportable": True}),
                (
                    "부산은?",
                    {
                        "status": "ok",
                        "figure": {**FIG, "label": "2020년 부산광역시 1인가구", "value": 455207},
                        "reportable": True,
                    },
                ),
                ("출생아랑 1인가구", {"status": "ok", "figure": FIG, "reportable": False}),
            ]
        ):
            turns.append(
                (
                    await conn.execute(
                        text(
                            "insert into app.chat_turns (session_id, user_id, turn_no, message,"
                            " status, recomputed, response)"
                            " values (:sid, :uid, :no, :msg, 'ok', true, cast(:resp as jsonb))"
                            " returning id"
                        ),
                        {
                            "sid": sid,
                            "uid": uid,
                            "no": i + 1,
                            "msg": q,
                            "resp": json.dumps(resp, ensure_ascii=False),
                        },
                    )
                ).scalar_one()
            )
    return sid, uid, turns


async def _teardown(uid):
    from app.core.db import engine

    async with engine.begin() as conn:
        await conn.execute(text("delete from app.chat_turns where user_id = :u"), {"u": uid})
        await conn.execute(text("delete from app.users where id = :u"), {"u": uid})


@pytest.fixture
def db():
    try:
        sid, uid, turns = run(_setup())
    except Exception as e:
        pytest.skip(f"app 스키마 DB 없음: {e}")
    yield sid, uid, turns
    run(_teardown(uid))


def script(monkeypatch, steps):
    """LLM 흉내: steps 를 차례로 돌려주고, 받은 메시지를 기록한다"""
    seen = []

    async def ask(messages):
        seen.append(messages)
        step = steps.pop(0)
        return step(messages) if callable(step) else step

    monkeypatch.setattr(ra, "_ask", ask)
    return seen


def act(action, **args):
    return {"reason": "", "args": args, "action": action}


# ---- 고리 ---------------------------------------------------------------------------------


def test_add_session_turns_and_finish(db, monkeypatch):
    sid, uid, (t1, t2, _) = db
    seen = script(
        monkeypatch, [act("add_item", turn_ids=[t1, t2]), act("finish", message="2개를 담았어요.")]
    )
    res = run(ra.run("서울이랑 부산 보고서에 담아줘", sid, {"turn_no": 3}, uid))
    # 담기에 성공하면 그 요청은 끝나고, 지금 보고서에 무엇이 있는지 코드가 말한다
    assert res["status"] == "report"
    assert res["answer"] == (
        "내 보고서에 2개가 있어요: 2020년 서울특별시 1인가구, 2020년 부산광역시 1인가구"
    )
    assert [i["turn_id"] for i in res["report"]["items"]] == [t1, t2]
    context = json.loads(seen[0][1]["content"])
    assert [t["turn_id"] for t in context["turns"]] == [t1, t2, _]
    assert "1390701" not in seen[0][1]["content"]  # 값은 포맷된 글로만 준다
    assert res["report_agent_state"]["pending"] is None


def test_unreportable_turn_is_refused_by_tool(db, monkeypatch):
    sid, uid, (_, _, bad) = db
    script(
        monkeypatch, [act("add_item", turn_ids=[bad]), act("refuse", why="unverified", message="")]
    )
    res = run(ra.run("이거 보고서에 넣어", sid, {"turn_no": 3}, uid))
    step = res["trace"]["report_agent"]["steps"][0]
    assert step["result"]["added"] == [] and "검증된 수치" in step["result"]["error"][0]
    assert res["answer"] == tools.REFUSE_WHY["unverified"]


def test_new_stat_needs_confirmation_before_adding(db, monkeypatch):
    sid, uid, _ = db
    made = []

    async def fake_handle(session_id, question, user_id=None, selections=None, listed=True):
        assert listed is False
        from app.core.db import engine

        async with engine.begin() as conn:
            tid = (
                await conn.execute(
                    text(
                        "insert into app.chat_turns (session_id, user_id, turn_no, message,"
                        " status, recomputed, response) values (:sid, :uid, 1, :q, 'ok', true,"
                        " cast(:r as jsonb)) returning id"
                    ),
                    {
                        "sid": f"{sid}-side",
                        "uid": user_id,
                        "q": question,
                        "r": json.dumps({"status": "ok", "figure": FIG, "reportable": True}),
                    },
                )
            ).scalar_one()
        made.append(tid)
        return {"status": "ok", "turn_id": tid, "figure": FIG, "reportable": True, "answer": "답"}

    from app.services import ask_session

    monkeypatch.setattr(ask_session, "handle", fake_handle)
    script(
        monkeypatch,
        [
            act("ask_stat", question="2015년 서울 1인 가구 수"),
            lambda m: act("add_item", turn_ids=[made[0]]),  # 확인 전에 담으려 함 → 도구가 막음
            act("ask_user", question="2015년 결과를 담을까요?", options=["담기", "그만두기"]),
        ],
    )
    first = run(ra.run("2015년 것도 넣어줘", sid, {"turn_no": 3}, uid))
    blocked = first["trace"]["report_agent"]["steps"][1]["result"]
    assert blocked["added"] == [] and "확인 전" in blocked["error"][0]
    assert first["clarification"] == {
        "kind": "report_choice",
        "prompt": "2015년 결과를 담을까요?",
        "options": ["담기", "그만두기"],
    }
    state = first["report_agent_state"]
    assert state["candidates"] == made and state["pending"]

    seen = script(
        monkeypatch,
        [lambda m: act("add_item", turn_ids=[made[0]]), act("finish", message="담았어요.")],
    )
    second = run(ra.run("응 담아", sid, {"turn_no": 4, "report_agent": state}, uid))
    assert [i["turn_id"] for i in second["report"]["items"]] == made
    # 확인을 기다리던 답을 이름 · 값과 함께 알려 준다 (다시 조회하지 않고 바로 담도록)
    waiting = json.loads(seen[0][1]["content"])["waiting_for_confirm"]
    assert waiting == [
        {
            "turn_id": made[0],
            "question": "2015년 서울 1인 가구 수",
            "label": FIG["label"],
            "value": "1,390,701가구",
            "reportable": True,
        }
    ]
    assert second["report_agent_state"]["pending"] is None


def test_ask_stat_limit(db, monkeypatch):
    sid, uid, _ = db
    from app.services import ask_session

    async def fake_handle(*a, **k):
        return {"status": "tables", "answer": "표 안내"}

    monkeypatch.setattr(ask_session, "handle", fake_handle)
    script(
        monkeypatch,
        [act("ask_stat", question=f"q{i}") for i in range(4)] + [act("finish", message="끝")],
    )
    res = run(ra.run("서울 1인 가구 보고서 써줘", sid, {"turn_no": 3}, uid))
    steps = res["trace"]["report_agent"]["steps"]
    assert "3번까지" in steps[3]["result"]["error"]
    assert [s["result"]["ask_stat_left"] for s in steps[:4]] == [2, 1, 0, 0]


def test_invalid_twice_asks_user(db, monkeypatch):
    sid, uid, _ = db
    script(monkeypatch, [None, {"action": "dance"}])
    res = run(ra.run("보고서", sid, {"turn_no": 3}, uid))
    assert res["clarification"]["kind"] == "report_choice"
    assert res["answer"] == "보고서에서 무엇을 할까요?"


def test_step_limit(db, monkeypatch):
    sid, uid, _ = db
    script(monkeypatch, [act("find_turns", text="서울")] * ra.MAX_STEPS)
    res = run(ra.run("보고서 비교해줘", sid, {"turn_no": 3}, uid))
    assert res["trace"]["report_agent"]["limit"] == "steps"
    assert "여기까지" in res["answer"]


def test_invented_numbers_in_message_are_replaced(db, monkeypatch):
    sid, uid, _ = db
    script(monkeypatch, [act("finish", message="서울은 약 140만 가구예요.")])
    res = run(ra.run("보고서에 뭐 있어?", sid, {"turn_no": 3}, uid))
    assert res["answer"] == ra.DONE_FALLBACK
    script(monkeypatch, [act("finish", message="서울은 1,390,701가구예요.")])
    ok = run(ra.run("보고서에 뭐 있어?", sid, {"turn_no": 3}, uid))
    assert ok["answer"] == "서울은 1,390,701가구예요."  # 보여 준 결과에 있던 숫자는 된다


def test_extra_args_are_dropped(db, monkeypatch):
    """HCX 가 args 에 action 같은 것을 섞어도 도구가 받는 인자만 넘긴다 (개발셋 R15-T4)"""
    sid, uid, (t1, _, _) = db
    script(
        monkeypatch,
        [
            {"reason": "", "args": {"turn_ids": [t1], "action": "add_item"}, "action": "add_item"},
            act("finish", message="담았어요."),
        ],
    )
    res = run(ra.run("서울 거 보고서에 담아줘", sid, {"turn_no": 3}, uid))
    assert res["trace"]["report_agent"]["steps"][0]["result"]["added"] == [t1]


def test_login_required():
    res = run(ra.run("보고서 만들어줘", "S", None, None))
    assert res["answer"] == tools.REFUSE_WHY["login"]


# ---- handle 에 붙인 경로 ---------------------------------------------------------------


def test_handle_routes_report_and_keeps_conditions(monkeypatch, ask_sessions):
    from app.services import ask_session as s

    async def fake_run(question, sid, state, user_id):
        return {
            "status": "report",
            "request_type": "report",
            "answer": "담았어요.",
            "parsed": {},
            "result": {},
            "sources": [],
            "question": question,
            "report": None,
            "trace": {"report_agent": {"steps": [{"action": "add_item"}], "end": "finish"}},
            "report_agent_state": {
                "pending": None,
                "candidates": [],
                "declined": False,
                "report_id": 9,
            },
        }

    monkeypatch.setattr(ra, "run", fake_run)
    before = {
        "conditions": {"indicator": "1인가구", "regions": ["서울"]},
        "turn_no": 2,
        "user_id": 3,
    }
    ask_sessions["sessions"]["R_1"] = before

    async def no_touch(*a):
        return None

    monkeypatch.setattr(s.conversations, "touch", no_touch)
    res = asyncio.run(s.handle("R_1", "이거 보고서에 담아줘", 3))
    assert res["request_type"] == "report" and res["session_id"] == "R_1"
    saved = ask_sessions["sessions"]["R_1"]
    assert saved["conditions"] == before["conditions"]  # 다음 "부산은?"이 이어지도록
    assert saved["turn_no"] == 3
    assert saved["report_agent"]["last_turn"] == 3 and saved["report_agent"]["report_id"] == 9


def test_handle_report_without_login_says_login_only(ask_sessions):
    """로그인하지 않고 '보고서 만들어줘': 오류 없이 로그인한 사용자만 만들 수 있다고 답한다"""
    from app.services import ask_session as s

    ask_sessions["sessions"]["G_1"] = {"conditions": {}, "turn_no": 2, "user_id": None}
    res = asyncio.run(s.handle("G_1", "보고서 만들어줘", None))
    assert res["request_type"] == "report" and res["session_id"] == "G_1"
    assert res["answer"] == tools.REFUSE_WHY["login"]
    assert ask_sessions["sessions"]["G_1"]["turn_no"] == 3


# ---- 보고서 제안 칩 (PR 6) ----------------------------------------------------------------


def _answer(indicator, region="서울특별시", reportable=True):
    fig = {"indicator": indicator, "conditions": {"region": region}}
    return {"reportable": reportable, "figure": fig}


def _play(answers, user_id=3, declined=False):
    from app.agent import report_offer

    state, offers = {"turn_no": 0, "report_agent": {"declined": declined}}, []
    for res in answers:
        after = {**state, "turn_no": state["turn_no"] + 1}
        report_offer.update(state, res, after, user_id)
        offers.append(res.get("report_offer"))
        state = after
    return offers, state


def test_offer_on_third_same_indicator_once():
    offers, state = _play([_answer("1인 가구 수") for _ in range(4)])
    assert offers[:2] == [None, None]
    assert offers[2] == {"topic": "서울특별시 1인 가구 수", "count": 3}
    assert offers[3] is None  # 한 대화에 한 번
    assert state["report_agent"]["offer_turn"] == 3
    assert router.wants("응 만들어줘", {"turn_no": 3, "report_agent": state["report_agent"]})


def test_offer_topic_without_region_when_regions_differ():
    offers, _ = _play([_answer("출생아 수", r) for r in ("서울특별시", "부산광역시", "대구광역시")])
    assert offers[2] == {"topic": "출생아 수", "count": 3}


@pytest.mark.parametrize(
    ("answers", "kw"),
    [
        ([_answer("총인구"), _answer("혼인 건수"), _answer("외국인주민 수")], {}),
        ([_answer("1인 가구 수") for _ in range(3)], {"declined": True}),
        ([_answer("1인 가구 수") for _ in range(3)], {"user_id": None}),
        ([_answer("1인 가구 수", reportable=False) for _ in range(3)], {}),
    ],
)
def test_no_offer(answers, kw):
    offers, _ = _play(answers, **kw)
    assert offers == [None, None, None]


# ---- 시키지 않은 일 막기 (개발셋 R03 · R04) ---------------------------------------------


def test_reorder_request_cannot_add(db, monkeypatch):
    sid, uid, (t1, t2, _) = db
    script(monkeypatch, [act("add_item", turn_ids=[t1]), act("finish", message="끝")])
    res = run(ra.run("혼인을 맨 앞으로", sid, {"turn_no": 3}, uid))
    step = res["trace"]["report_agent"]["steps"][0]
    assert "담아 달라고 하지 않았습니다" in step["result"]["error"]
    assert res["report"] is None


def test_add_request_cannot_write_report(db, monkeypatch):
    sid, uid, (t1, _, _) = db
    script(
        monkeypatch,
        [act("add_item", turn_ids=[t1]), act("write_report"), act("finish", message="담았어요.")],
    )
    res = run(ra.run("서울 1인 가구 보고서에 담아줘", sid, {"turn_no": 3}, uid))
    steps = res["trace"]["report_agent"]["steps"]
    assert steps[0]["result"]["added"] == [t1]
    assert len(steps) == 1  # 담기에서 끝나 write_report 까지 가지 않는다
    assert res["report"]["status"] == "draft"


def test_last_answer_is_marked(db, monkeypatch):
    sid, uid, (_, _, t3) = db
    seen = script(monkeypatch, [act("finish", message="끝")])
    run(ra.run("그거 넣어줘", sid, {"turn_no": 3}, uid))
    assert json.loads(seen[0][1]["content"])["last_answer_turn_id"] == t3


def test_pointed_word_adds_only_last_answer(db, monkeypatch):
    sid, uid, (t1, t2, _) = db

    async def two_answers(ctx, limit=20):
        rows = [
            {"turn_id": t, "question": "q", "label": "l", "value": "v", "reportable": True}
            for t in (t1, t2)
        ]
        ctx.session_ids = {t1, t2}
        return rows

    monkeypatch.setattr(ra.tools, "session_turns", two_answers)
    script(monkeypatch, [act("add_item", turn_ids=[t1, t2]), act("finish", message="담았어요.")])
    res = run(ra.run("그거 넣어줘", sid, {"turn_no": 3}, uid))
    step = res["trace"]["report_agent"]["steps"][0]
    assert step["result"]["added"] == [t2]
    assert "바로 앞 답" in step["result"]["error"][0]
    script(monkeypatch, [act("add_item", turn_ids=[t1, t2]), act("finish", message="담았어요.")])
    res = run(ra.run("그거 다 넣어줘", sid, {"turn_no": 4}, uid))
    assert "error" not in res["trace"]["report_agent"]["steps"][0]["result"]  # "다"면 여럿
    assert [i["turn_id"] for i in res["report"]["items"]] == [t2, t1]


def test_same_failing_action_twice_stops(db, monkeypatch):
    sid, uid, _ = db
    script(monkeypatch, [act("write_report")] * ra.MAX_STEPS)
    res = run(ra.run("순서 바꿔줘", sid, {"turn_no": 3}, uid))
    assert [s["action"] for s in res["trace"]["report_agent"]["steps"]] == ["write_report"] * 2
    assert res["answer"] == ra.ACTION_FAILED


def test_report_agent_edits_only_the_current_conversation(db, monkeypatch):
    sid, uid, (t1, _, _) = db
    current = run(reports.create(uid, title="현재 대화", turn_ids=[t1], session_id=sid))
    other = run(reports.create(uid, title="다른 대화", session_id=f"{sid}-other"))
    script(monkeypatch, [act("set_title", title="수정된 보고서")])

    res = run(ra.run("보고서 제목을 바꿔줘", sid, {"turn_no": 3}, uid))

    assert res["report"]["id"] == current["id"]
    assert res["report"]["title"] == "수정된 보고서"
    assert run(reports.detail(uid, other["id"]))["title"] == "다른 대화"


@pytest.mark.parametrize("can_shorten", [True, False])
def test_revise_is_terminal_and_reports_the_actual_outcome(db, monkeypatch, can_shorten):
    sid, uid, (t1, _, _) = db
    draft = run(reports.create(uid, turn_ids=[t1], session_id=sid))
    ready = {**draft, "status": "ready"}
    generated = []

    async def latest(user, session):
        assert (user, session) == (uid, sid)
        return ready

    async def generate(user, report_id, style, *, shorten=False):
        generated.append((user, report_id, style, shorten))
        if not can_shorten:
            raise reports.ReportError(reports.NOT_SHORTER, reports.ALREADY_SHORT)
        return {**ready, "content": {"summary": [{"text": "짧은 요약"}]}}

    monkeypatch.setattr(reports, "latest_for_conversation", latest)
    monkeypatch.setattr(reports, "generate", generate)
    calls = script(monkeypatch, [act("revise_text", style="한 문장"), act("write_report")])
    res = run(ra.run("보고서 좀 짧게 해줘", sid, {"turn_no": 3}, uid))

    assert generated == [(uid, draft["id"], "한 문장", True)]
    assert len(calls) == 1
    if can_shorten:
        assert res["answer"] == "보고서를 더 짧게 줄였어요."
        assert res["report"]["content"]["summary"] == [{"text": "짧은 요약"}]
    else:
        assert res["answer"] == reports.ALREADY_SHORT
        assert res["report"] == ready


def test_correction_can_add():
    from app.agent import report_tools as t

    assert t.ASKS_ADD.search("아니 서울 거") and t.ASKS_ADD.search("부산 말고 서울")
    assert not t.ASKS_ADD.search("혼인을 맨 앞으로")


def test_find_turns_accepts_text_arg(db, monkeypatch):
    """프롬프트대로 {"text": ...}를 보내도 인자 오류가 나지 않는다"""
    sid, uid, (t1, _, _) = db
    seen = script(
        monkeypatch, [act("find_turns", text="서울 1인 가구"), act("finish", message="끝")]
    )
    res = run(ra.run("아까 서울 거 보고서에 담아줘", sid, {"turn_no": 3}, uid))
    result = res["trace"]["report_agent"]["steps"][0]["result"]
    assert "error" not in result and t1 in [c["turn_id"] for c in result["candidates"]]
    assert len(seen) == 2


def test_offer_topic_ignores_age_sex_and_population():
    from app.agent import report_offer

    names = [
        "1인 가구 수",
        "여성 1인가구",
        "70세 이상 1인가구(20세이상 인구) 수",
        "20~29세 1인 가구",
    ]
    assert {report_offer._topic({"indicator": n}) for n in names} == {"1인가구"}
    assert report_offer._topic({"indicator": "출생아 수"}) != report_offer._topic(
        {"indicator": "사망자 수"}
    )


def test_correction_replaces_last_added(db, monkeypatch):
    """'아니 서울 거': 앞 요청에서 담은 부산을 빼고 서울을 담는다"""
    sid, uid, (t1, t2, _) = db
    script(monkeypatch, [act("add_item", turn_ids=[t2]), act("finish", message="담았어요.")])
    first = run(ra.run("부산 거 보고서에 담아줘", sid, {"turn_no": 3}, uid))
    state = first["report_agent_state"]
    assert state["last_added"] == [t2]
    script(monkeypatch, [act("add_item", turn_ids=[t1]), act("finish", message="바꿨어요.")])
    second = run(ra.run("아니 서울 거", sid, {"turn_no": 4, "report_agent": state}, uid))
    step = second["trace"]["report_agent"]["steps"][0]["result"]
    assert step["removed_as_correction"] == [t2]
    assert [i["turn_id"] for i in second["report"]["items"]] == [t1]
    # LLM 이 앞에서 담은 것까지 다시 골라도 바꿔 담는다
    script(monkeypatch, [act("add_item", turn_ids=[t2]), act("finish", message="담았어요.")])
    state = second["report_agent_state"]
    third = run(ra.run("부산 거 담아줘", sid, {"turn_no": 5, "report_agent": state}, uid))
    script(monkeypatch, [act("add_item", turn_ids=[t1, t2]), act("finish", message="바꿨어요.")])
    state = third["report_agent_state"]
    fourth = run(ra.run("아니 서울 거", sid, {"turn_no": 6, "report_agent": state}, uid))
    assert [i["turn_id"] for i in fourth["report"]["items"]] == [t1]


def test_read_action_tolerates_two_actions_in_args():
    raw = (
        '{"reason": "r", "args": {"remove_item": {"turn_ids": [5]}, '
        '"find_turns": {"text": "서울"}}}, "action": "remove_item"}'
    )
    assert ra.read_action(raw) == {
        "reason": "r",
        "args": {"turn_ids": [5]},
        "action": "remove_item",
    }
    assert ra.read_action('{"args": {}, "action": "finish"}')["action"] == "finish"
    assert ra.read_action("not json") is None


def test_small_edit_ends_request(db, monkeypatch):
    """담기가 성공하면 그 요청은 끝낸다: 맞게 담은 뒤 다른 행동으로 망치지 않게 (개발셋 R15)"""
    sid, uid, (t1, t2, _) = db
    seen = script(monkeypatch, [act("add_item", turn_ids=[t1]), act("remove_item", turn_ids=[t1])])
    res = run(ra.run("서울 거 보고서에 담아줘", sid, {"turn_no": 3}, uid))
    assert [s["action"] for s in res["trace"]["report_agent"]["steps"]] == ["add_item"]
    assert len(seen) == 1
    assert res["answer"] == "내 보고서에 1개가 있어요: 2020년 서울특별시 1인가구"
    # 만들어 달라고 했으면 이어 간다
    script(monkeypatch, [act("add_item", turn_ids=[t2]), act("finish", message="만들었어요.")])
    res = run(ra.run("부산 것도 넣어서 보고서 만들어줘", sid, {"turn_no": 4}, uid))
    assert [s["action"] for s in res["trace"]["report_agent"]["steps"]] == ["add_item", "finish"]


# ---- 한 대화의 답만 (#253) ------------------------------------------------------------------


def test_asked_stat_joins_this_conversation_and_can_be_added(db, monkeypatch):
    """따로 조회한 답은 이 대화의 답으로 옮겨서, 한 대화 규칙에 막히지 않고 담긴다"""
    sid, uid, _ = db
    from app.services import ask_session

    made = []

    async def fake_handle(session_id, question, user_id=None, selections=None, listed=True):
        from app.core.db import engine

        async with engine.begin() as conn:
            tid = (
                await conn.execute(
                    text(
                        "insert into app.chat_turns (session_id, user_id, turn_no, message,"
                        " status, recomputed, response) values ('other-side', :uid, 1, :q,"
                        " 'ok', true, cast(:r as jsonb)) returning id"
                    ),
                    {
                        "uid": user_id,
                        "q": question,
                        "r": json.dumps({"status": "ok", "figure": FIG, "reportable": True}),
                    },
                )
            ).scalar_one()
        made.append(tid)
        return {"status": "ok", "turn_id": tid, "figure": FIG, "reportable": True, "answer": "답"}

    monkeypatch.setattr(ask_session, "handle", fake_handle)
    script(
        monkeypatch,
        [
            act("ask_stat", question="2015년 서울 1인 가구 수"),
            act("ask_user", question="담을까요?", options=["담기"]),
        ],
    )
    first = run(ra.run("2015년 것도 넣어줘", sid, {"turn_no": 3}, uid))

    async def session_of(tid):
        from app.core.db import engine

        async with engine.begin() as conn:
            return (
                await conn.execute(
                    text("select session_id from app.chat_turns where id = :t"), {"t": tid}
                )
            ).scalar()

    assert run(session_of(made[0])) == sid
    script(monkeypatch, [act("add_item", turn_ids=made), act("finish", message="담았어요.")])
    second = run(
        ra.run("응 담아", sid, {"turn_no": 4, "report_agent": first["report_agent_state"]}, uid)
    )
    assert [i["turn_id"] for i in second["report"]["items"]] == made
    assert second["report"]["session_id"] == sid


def test_find_turns_stays_in_this_conversation(db, monkeypatch):
    sid, uid, (t1, _, _) = db

    async def other_turn():
        from app.core.db import engine

        async with engine.begin() as conn:
            return (
                await conn.execute(
                    text(
                        "insert into app.chat_turns (session_id, user_id, turn_no, message, status,"
                        " recomputed, response) values ('another-talk', :uid, 1, '어제 출생아 수',"
                        " 'ok', true, cast(:r as jsonb)) returning id"
                    ),
                    {
                        "uid": uid,
                        "r": json.dumps({"status": "ok", "figure": FIG, "reportable": True}),
                    },
                )
            ).scalar_one()

    other = run(other_turn())
    script(monkeypatch, [act("find_turns", text="출생아"), act("refuse", why="other_talk")])
    res = run(ra.run("어제 본 출생아 수 보고서에 넣어줘", sid, {"turn_no": 3}, uid))
    found = res["trace"]["report_agent"]["steps"][0]["result"]
    assert other not in [c["turn_id"] for c in found["candidates"]]
    assert res["answer"] == ra.tools.REFUSE_WHY["other_talk"]


# ---- 개발셋 v0.3 오답 (A · C · D · E · H) ---------------------------------------------------


def _fake_generate(monkeypatch, calls):
    """보고서 문장 생성(LLM) 흉내: 받은 style 을 기록하고 ready 로 둔다"""
    from app.services import reports

    async def generate(user_id, report_id, style=None):
        from app.core.db import engine

        calls.append(style)
        async with engine.begin() as conn:
            await conn.execute(
                text("update app.reports set status = 'ready' where id = :r"), {"r": report_id}
            )
        report = await reports.detail(user_id, report_id)
        return {**report, "content": {"summary": [], "limits": []}}

    monkeypatch.setattr(reports, "generate", generate)


def test_confirm_after_write_request_keeps_going_to_write(db, monkeypatch):
    """A: "보고서 만들어줘" → 되묻기 → "응 넣어"는 담은 뒤 보고서까지 만든다
    (개발셋 v0.3 R05 · R13 · R16 · R18)"""
    sid, uid, (t1, _, _) = db
    calls = []
    _fake_generate(monkeypatch, calls)
    script(monkeypatch, [act("ask_user", question="담을까요?", options=["네", "아니요"])])
    first = run(ra.run("서울 1인 가구 보고서 만들어줘", sid, {"turn_no": 3}, uid))
    state = first["report_agent_state"]
    assert state["pending"] and state["wants_write"] is True

    seen = script(
        monkeypatch,
        [act("add_item", turn_ids=[t1]), act("write_report"), act("finish", message="만들었어요.")],
    )
    second = run(ra.run("응 넣어", sid, {"turn_no": 4, "report_agent": state}, uid))
    actions = [s["action"] for s in second["trace"]["report_agent"]["steps"]]
    assert actions == ["add_item", "write_report", "finish"]  # 담기에서 끝나지 않고 만든다
    assert "error" not in second["trace"]["report_agent"]["steps"][1]["result"]
    assert calls == [None]
    assert json.loads(seen[0][1]["content"])["report_requested"] is True
    assert second["report_agent_state"]["wants_write"] is False  # 되묻지 않았으면 넘기지 않는다


def test_confirm_after_add_request_does_not_write(db, monkeypatch):
    """A: 담기만 해 달라고 했으면 되물은 뒤에도 만들지 않는다"""
    sid, uid, (t1, _, _) = db
    script(monkeypatch, [act("ask_user", question="담을까요?", options=["네"])])
    first = run(ra.run("2015년 것도 넣어줘", sid, {"turn_no": 3}, uid))
    assert first["report_agent_state"]["wants_write"] is False
    script(monkeypatch, [act("add_item", turn_ids=[t1]), act("write_report")])
    second = run(
        ra.run("응 넣어", sid, {"turn_no": 4, "report_agent": first["report_agent_state"]}, uid)
    )
    assert [s["action"] for s in second["trace"]["report_agent"]["steps"]] == ["add_item"]


def test_revise_without_write_words_and_ends(db, monkeypatch):
    """C · D: "결론부터 나오게 해줘"도 고치고, 고친 뒤에는 끝낸다 (R05-T2 · R07-T4 · R07-T5)"""
    sid, uid, (t1, _, _) = db
    calls = []
    _fake_generate(monkeypatch, calls)
    script(monkeypatch, [act("add_item", turn_ids=[t1]), act("write_report"), act("finish")])
    made = run(ra.run("서울 거 넣어서 보고서 만들어줘", sid, {"turn_no": 3}, uid))
    assert made["report"]["status"] == "ready"

    seen = script(monkeypatch, [act("revise_text"), act("write_report"), act("finish")])
    res = run(ra.run("결론부터 나오게 해줘", sid, {"turn_no": 4}, uid))
    steps = res["trace"]["report_agent"]["steps"]
    assert [s["action"] for s in steps] == ["revise_text"]
    assert "error" not in steps[0]["result"]
    assert len(seen) == 1  # 다시 만들어 덮어쓰지 않는다
    assert calls == [None, "결론부터 나오게 해줘"]  # style 이 비면 사용자 말로 고친다
    assert res["answer"] == "보고서 문장을 수정했어요."


def test_empty_add_item_is_an_error(db, monkeypatch):
    """H: turn_ids 없는 add_item 은 오류로 알린다 (R08-T2). '그거'면 바로 앞 답"""
    sid, uid, (_, _, t3) = db
    script(monkeypatch, [act("add_item"), act("finish", message="끝")])
    res = run(ra.run("보고서에 넣고 왜 늘었는지도 써줘", sid, {"turn_no": 3}, uid))
    step = res["trace"]["report_agent"]["steps"][0]
    assert "turn_ids 가 비었습니다" in step["result"]["error"]

    script(monkeypatch, [act("add_item"), act("finish", message="끝")])
    res = run(ra.run("그거 넣어줘", sid, {"turn_no": 4}, uid))
    step = res["trace"]["report_agent"]["steps"][0]
    assert step["result"]["added"] == [] and "검증된 수치" in step["result"]["error"][0]
    # 바로 앞 답(t3)을 담으려 함
    assert t3 in [int(x.split(":")[0]) for x in step["result"]["error"]]


@pytest.mark.parametrize(
    "question, expected",
    [
        ("어제 본 출생아 수 보고서에 넣어줘", tools.REFUSE_WHY["other_talk"]),
        ("숫자는 140만 가구처럼 대충 써줘", tools.REFUSE_WHY["numbers"]),
        ("더 짧게 써줘", ra.REFUSE_FALLBACK),
    ],
)
def test_refuse_without_why_explains(db, monkeypatch, question, expected):
    """E: refuse 를 이유 없이 보내도 '처리했어요'가 아니라 거절 이유를 말한다 (R07-T6 · R18-T1)"""
    sid, uid, _ = db
    script(monkeypatch, [act("refuse")])
    res = run(ra.run(question, sid, {"turn_no": 3}, uid))
    assert res["answer"] == expected


def test_decline_offer_answer(db, monkeypatch):
    """E: 제안 거절에 '내 보고서에서 확인해 주세요'라고 하지 않는다 (R14-T4)"""
    sid, uid, _ = db
    script(monkeypatch, [act("decline_offer"), act("finish")])
    res = run(ra.run("괜찮아 보고서는 됐어", sid, {"turn_no": 3}, uid))
    assert res["answer"] == ra.DECLINED
