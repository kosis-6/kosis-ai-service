"""통합 보고서 초안: 서비스(실제 DB의 app 스키마) · API (명세서 4 · 6장)

서비스 테스트는 통계 데이터가 필요 없어 CI(빈 DB + alembic upgrade head)에서도 돈다.
사용자 두 명과 chat_turns 행을 만들고 끝나면 지운다.
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.deps import get_current_user
from app.core.db import get_session
from app.main import app
from app.services import ask_session, conversations
from app.services import reports as r
from app.services.reports import ReportError

FIG = {"label": "2020년 서울 1인가구", "value": 1, "unit": "가구", "kind": "value"}
ASK_STORAGE = {
    name: getattr(ask_session, name) for name in ("load", "save", "log_turn", "turn_writer")
}


def run(coro):
    async def go():
        from app.core.db import engine

        try:
            return await coro
        finally:
            await engine.dispose()  # asyncio.run마다 새 이벤트 루프라 연결을 넘기지 않는다

    return asyncio.run(go())


async def _setup():
    from app.core.db import engine

    tag = uuid.uuid4().hex[:10]
    async with engine.begin() as conn:
        users = []
        for who in ("a", "b"):
            row = await conn.execute(
                text("insert into app.users (email, password_hash) values (:e, 'x') returning id"),
                {"e": f"report-test-{who}-{tag}@example.com"},
            )
            users.append(row.scalar_one())
        turns = {}
        rows = [
            (
                "ok1",
                users[0],
                {
                    "status": "ok",
                    "answer": "답1",
                    "figure": FIG,
                    "reportable": True,
                    "chart": {"type": "line", "rows": [{"label": "2024", "value": 10}]},
                },
            ),
            ("ok2", users[0], {"status": "ok", "answer": "답2", "figure": FIG, "reportable": True}),
            ("ok3", users[0], {"status": "ok", "answer": "답3", "figure": FIG, "reportable": True}),
            ("bad", users[0], {"status": "ok", "answer": "일부만", "figure": FIG}),
            ("tables", users[0], {"status": "tables", "answer": "표 안내"}),
            (
                "other",
                users[1],
                {"status": "ok", "answer": "남의 답", "figure": FIG, "reportable": True},
            ),
            # 같은 사용자의 다른 대화창 답: 한 보고서에 섞지 않는다
            (
                "talk2",
                users[0],
                {"status": "ok", "answer": "다른 대화 답", "figure": FIG, "reportable": True},
            ),
        ]
        for i, (key, uid, response) in enumerate(rows):
            row = await conn.execute(
                text(
                    "insert into app.chat_turns (session_id, user_id, turn_no, message, status,"
                    " recomputed, response) values (:sid, :uid, :no, :msg, 'ok', true,"
                    " cast(:resp as jsonb)) returning id"
                ),
                {
                    "sid": f"report-test-{tag}" + ("-2" if key == "talk2" else ""),
                    "uid": uid,
                    "no": i + 1,
                    "msg": f"질문 {key}",
                    "resp": json.dumps(response, ensure_ascii=False),
                },
            )
            turns[key] = row.scalar_one()
    return tag, users, turns


async def _teardown(tag, users):
    from app.core.db import engine

    async with engine.begin() as conn:
        await conn.execute(
            text("delete from app.chat_turns where session_id like :sid"),
            {"sid": f"report-test-{tag}%"},
        )
        await conn.execute(text("delete from app.users where id = any(:ids)"), {"ids": users})


@pytest.fixture
def db():
    try:
        tag, users, turns = run(_setup())
    except Exception as e:  # DB가 없거나 마이그레이션 전
        pytest.skip(f"app 스키마 DB 없음: {e}")
    yield SimpleNamespace(a=users[0], b=users[1], t=turns, talk=f"report-test-{tag}")
    run(_teardown(tag, users))


def test_create_with_items_copies_snapshot(db):
    rep = run(r.create(db.a, " 서울  1인 가구 ", "규모", [db.t["ok1"], db.t["ok2"], db.t["ok1"]]))
    assert rep["title"] == "서울 1인 가구"
    assert rep["status"] == "draft"
    assert [i["turn_id"] for i in rep["items"]] == [db.t["ok1"], db.t["ok2"]]
    snap = rep["items"][0]["snapshot"]
    assert snap["question"] == "질문 ok1"
    assert snap["figure"] == FIG
    assert snap["answer"] == "답1"
    assert "reportable" not in snap


def test_snapshot_survives_turn_deletion(db):
    rep = run(r.create(db.a, None, None, [db.t["ok1"]]))

    async def wipe():
        from app.core.db import engine

        async with engine.begin() as conn:
            await conn.execute(text("delete from app.chat_turns where id = :i"), {"i": db.t["ok1"]})

    run(wipe())
    again = run(r.detail(db.a, rep["id"]))
    assert again["items"][0]["snapshot"]["answer"] == "답1"
    assert again["title"] == r.DEFAULT_TITLE


def test_create_saves_selected_chart_view_without_changing_the_answer(db):
    selected = run(r.create(db.a, turn_ids=[db.t["ok1"]], chart_views={db.t["ok1"]: "bar"}))
    original = run(r.create(db.a, turn_ids=[db.t["ok1"]]))
    saved = run(r.detail(db.a, selected["id"]))
    chart = original["items"][0]["snapshot"]["chart"]
    assert "view" not in chart
    assert saved["items"][0]["snapshot"]["chart"] == {**chart, "view": "bar"}


def test_chart_view_chosen_on_screen_reaches_every_report_path(db):
    """화면에서 바꾼 모양은 그 답에 남아, 말로 만든 보고서(담기 · '정리해줘')도 그 모양으로 그린다.
    [보고서] 시트만 chart_views 를 보내 '보고서 만들어줘'로 만들면 꺾은선으로 돌아갔다"""
    assert run(conversations.set_chart_view(db.a, db.talk, db.t["ok1"], "bar")) == "ok"
    drafted = run(r.add_to_draft(db.a, db.t["ok1"]))  # 보고서 에이전트 · 제안 칩의 담기
    assert drafted["items"][0]["snapshot"]["chart"]["view"] == "bar"
    made = run(r.create(db.a, turn_ids=[db.t["ok1"]]))  # '정리해줘' 보고서
    assert made["items"][0]["snapshot"]["chart"]["view"] == "bar"
    assert run(_response(db.t["ok1"]))["chart"]["view"] == "bar"  # 다시 연 대화가 그리는 답


@pytest.mark.parametrize("legacy", [False, True])
def test_detail_reuses_the_latest_answers_saved_chart_view(db, legacy, monkeypatch):
    for name, service in ASK_STORAGE.items():
        monkeypatch.setattr(ask_session, name, service)
    monkeypatch.setattr(ask_session.indicators, "find_in_text", lambda _: [])
    monkeypatch.setattr(ask_session.indicators, "find_regions", lambda _: [])

    async def exercise():
        sid = db.talk + "-detail"
        question = "부산 출생아 수 추이를 알려줘"
        chart = {
            "type": "line",
            "title": "부산 출생아 수",
            "category": "연도",
            "unit": "명",
            "rows": [{"label": "2023", "value": 10}, {"label": "2024", "value": 12}],
        }
        first = {
            "status": "ok",
            "request_type": "new",
            "answer": "출생아 수 추이",
            "chart": chart,
            "parsed": {"type": "trend", "indicator": "출생아 수", "regions": ["부산광역시"]},
        }
        latest = {**first, "chart": dict(chart)}
        before = ask_session.next_state(None, question, first, {})
        before["user_id"] = db.a
        after = {**ask_session.next_state(before, question, latest, {}), "user_id": db.a}
        try:
            await ask_session._save(sid, before, first, None, question, db.a)
            await ask_session._save(sid, after, latest, before, question, db.a)
            if legacy:
                after["last"].pop("chart_turn_id")
                await ask_session.save(sid, after)
            assert await conversations.set_chart_view(db.a, sid, latest["turn_id"], "bar") == "ok"
            assert await conversations.set_chart_view(db.a, sid, first["turn_id"], "table") == "ok"

            detail = await ask_session.handle(sid, "자세히 말해줘", db.a)
            assert detail["request_type"] == "detail"
            assert detail["chart"] == {**chart, "view": "bar"}
            history = await conversations.detail(db.a, sid)
            assert history["turns"][-1]["response"]["turn_id"] == detail["turn_id"]

            assert await conversations.set_chart_view(db.a, sid, detail["turn_id"], "table") == "ok"
            ask_session.STATE_CACHE.delete(sid)
            again = await ask_session.handle(sid, "자세히 말해줘", db.a)
            assert again["chart"] == {**chart, "view": "table"}
        finally:
            await conversations.delete(db.a, sid)

    run(exercise())


async def _response(turn_id):
    from app.core.db import engine

    async with engine.begin() as conn:
        row = await conn.execute(
            text("select response from app.chat_turns where id = :t"), {"t": turn_id}
        )
        return row.scalar()


def test_chart_view_is_kept_only_for_own_drawable_charts(db):
    assert run(conversations.set_chart_view(db.b, db.talk, db.t["ok1"], "bar")) == "missing"
    assert run(conversations.set_chart_view(db.a, db.talk, db.t["ok2"], "bar")) == "missing"
    assert run(conversations.set_chart_view(db.a, "other-talk", db.t["ok1"], "bar")) == "missing"


@pytest.mark.parametrize(
    ("key", "code"),
    [("bad", "not_reportable"), ("tables", "not_reportable"), ("other", "not_found")],
)
def test_cannot_add_unreportable_or_others_turn(db, key, code):
    rep = run(r.create(db.a))
    with pytest.raises(ReportError) as e:
        run(r.add_item(db.a, rep["id"], db.t[key]))
    assert e.value.code == code
    assert run(r.detail(db.a, rep["id"]))["items"] == []


def test_create_with_bad_turn_leaves_nothing(db):
    with pytest.raises(ReportError):
        run(r.create(db.a, "x", None, [db.t["ok1"], db.t["bad"]]))
    assert run(r.listing(db.a))["total"] == 0


def test_add_is_idempotent_and_remove(db):
    rep = run(r.create(db.a))
    run(r.add_item(db.a, rep["id"], db.t["ok1"]))
    rep = run(r.add_item(db.a, rep["id"], db.t["ok1"]))
    assert len(rep["items"]) == 1
    rep = run(r.remove_item(db.a, rep["id"], db.t["ok1"]))
    assert rep["items"] == []


def test_item_limit(db, monkeypatch):
    monkeypatch.setattr(r, "MAX_ITEMS", 2)
    rep = run(r.create(db.a, None, None, [db.t["ok1"], db.t["ok2"]]))
    with pytest.raises(ReportError) as e:
        run(r.add_item(db.a, rep["id"], db.t["ok3"]))
    assert e.value.code == "full"


def test_reorder_and_rename(db):
    rep = run(r.create(db.a, None, None, [db.t["ok1"], db.t["ok2"], db.t["ok3"]]))
    order = [db.t["ok3"], db.t["ok1"], db.t["ok2"]]
    rep = run(r.update(db.a, rep["id"], title="새 제목", purpose="목적", order=order))
    assert [i["turn_id"] for i in rep["items"]] == order
    assert (rep["title"], rep["purpose"]) == ("새 제목", "목적")
    with pytest.raises(ReportError) as e:
        run(r.update(db.a, rep["id"], order=[db.t["ok1"], db.t["ok2"]]))
    assert e.value.code == "bad_order"


def test_item_change_clears_generated_content(db):
    rep = run(r.create(db.a, None, None, [db.t["ok1"]]))

    async def generated():
        from app.core.db import engine

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "update app.reports set status = 'ready', content = '{\"x\": 1}' where id = :i"
                ),
                {"i": rep["id"]},
            )

    run(generated())
    rep = run(r.add_item(db.a, rep["id"], db.t["ok2"]))
    assert rep["status"] == "draft" and rep["content"] is None


def test_draft_is_reused_then_new_after_generation(db):
    first = run(r.add_to_draft(db.a, db.t["ok1"], db.talk))
    second = run(r.add_to_draft(db.a, db.t["ok2"]))
    assert first["id"] == second["id"] and len(second["items"]) == 2
    assert second["session_id"] == db.talk  # 대화는 답에서 정한다

    async def generated():
        from app.core.db import engine

        async with engine.begin() as conn:
            await conn.execute(
                text("update app.reports set status = 'ready', content = '{}' where id = :i"),
                {"i": first["id"]},
            )

    run(generated())
    third = run(r.add_to_draft(db.a, db.t["ok3"]))
    assert third["id"] != first["id"]


# ---- 한 대화의 답만 (대화창 [보고서]에서 그 대화의 답을 골라 만든다) --------------------


def test_report_takes_its_conversation_from_the_answers(db):
    rep = run(r.create(db.a, turn_ids=[db.t["ok1"], db.t["ok2"]]))
    assert rep["session_id"] == db.talk  # 화면이 보내지 않아도 답에서 정한다


def test_answers_of_another_conversation_are_refused(db):
    """여러 대화의 답을 섞지 않는다 (11/26 결정: 한 대화창 안에서 골라 만든다)"""
    with pytest.raises(ReportError) as e:
        run(r.create(db.a, turn_ids=[db.t["ok1"], db.t["talk2"]]))
    assert e.value.code == r.OTHER_TALK
    assert run(r.listing(db.a))["total"] == 0  # 섞인 요청은 아무것도 만들지 않는다
    rep = run(r.create(db.a, turn_ids=[db.t["ok1"]]))
    with pytest.raises(ReportError) as e:
        run(r.add_item(db.a, rep["id"], db.t["talk2"]))
    assert e.value.code == r.OTHER_TALK
    assert [i["turn_id"] for i in run(r.detail(db.a, rep["id"]))["items"]] == [db.t["ok1"]]


def test_session_sent_by_the_screen_must_match_the_answers(db):
    with pytest.raises(ReportError) as e:
        run(r.create(db.a, turn_ids=[db.t["ok1"]], session_id="다른-대화"))
    assert e.value.code == r.OTHER_TALK
    with pytest.raises(ReportError):
        run(r.add_to_draft(db.a, db.t["ok1"], "다른-대화"))


def test_draft_is_kept_per_conversation(db):
    """[보고서에 담기]가 사용자의 가장 최근 초안에 담아 다른 대화의 답과 섞였다"""
    first = run(r.add_to_draft(db.a, db.t["ok1"]))
    other = run(r.add_to_draft(db.a, db.t["talk2"]))
    again = run(r.add_to_draft(db.a, db.t["ok2"]))
    assert other["id"] != first["id"] and again["id"] == first["id"]
    assert [i["turn_id"] for i in again["items"]] == [db.t["ok1"], db.t["ok2"]]
    assert [i["turn_id"] for i in other["items"]] == [db.t["talk2"]]


def test_empty_report_takes_the_conversation_of_its_first_answer(db):
    rep = run(r.create(db.a))
    assert rep["session_id"] is None
    rep = run(r.add_item(db.a, rep["id"], db.t["ok1"]))
    assert rep["session_id"] == db.talk
    with pytest.raises(ReportError):
        run(r.add_item(db.a, rep["id"], db.t["talk2"]))


def test_concurrent_first_adds_make_one_draft(db):
    """초안이 없을 때 같은 답을 동시에 두 번 담아도 초안은 하나 (#245 리뷰)"""

    async def both():
        return await asyncio.gather(
            r.add_to_draft(db.a, db.t["ok1"]), r.add_to_draft(db.a, db.t["ok1"])
        )

    first, second = run(both())
    assert first["id"] == second["id"]
    assert run(r.listing(db.a))["total"] == 1
    assert len(run(r.detail(db.a, first["id"]))["items"]) == 1


def test_draft_not_created_for_unreportable(db):
    with pytest.raises(ReportError):
        run(r.add_to_draft(db.a, db.t["bad"]))
    assert run(r.listing(db.a))["total"] == 0


def test_other_user_sees_nothing(db):
    rep = run(r.create(db.a, None, None, [db.t["ok1"]]))
    assert run(r.detail(db.b, rep["id"])) is None
    assert run(r.listing(db.b))["total"] == 0
    with pytest.raises(ReportError):
        run(r.add_item(db.b, rep["id"], db.t["other"]))
    assert run(r.delete(db.b, rep["id"])) is False
    assert run(r.delete(db.a, rep["id"])) is True


def test_title_masks_secrets():
    assert r.clean_title("  ") == r.DEFAULT_TITLE
    assert len(r.clean_title("가" * 200)) == r.TITLE_MAX


# ---- API (서비스는 흉내) ------------------------------------------------------------

READ = {
    "id": 5,
    "session_id": None,
    "title": "t",
    "purpose": None,
    "status": "draft",
    "content": None,
    "created_at": "2026-11-26T00:00:00Z",
    "updated_at": "2026-11-26T00:00:00Z",
    "items": [],
}


@pytest.fixture
def client(monkeypatch):
    calls = []

    def fake(name, result=READ, error=None):
        async def f(*args, **kw):
            calls.append((name, args))
            if error:
                raise error
            return result

        monkeypatch.setattr(r, name, f)

    for name in ("create", "add_item", "add_to_draft", "remove_item", "update"):
        fake(name)
    fake("detail", None)
    fake("delete", False)
    fake("listing", {"total": 0, "reports": []})

    async def no_db():
        yield None

    app.dependency_overrides[get_session] = no_db
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=3)
    c = TestClient(app)
    c.calls, c.fake = calls, fake
    yield c
    app.dependency_overrides.clear()


def test_api_routes_pass_user(client):
    assert client.post("/reports", json={"title": "x", "turn_ids": [1, 2]}).status_code == 201
    assert client.post("/reports/draft/items", json={"turn_id": 9}).status_code == 200
    assert client.put("/reports/5/items/9").status_code == 200
    assert client.delete("/reports/5/items/9").status_code == 200
    assert client.patch("/reports/5", json={"order": [9]}).status_code == 200
    assert client.get("/reports").json() == {"total": 0, "reports": []}
    assert all(args[0] == 3 for _, args in client.calls)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("not_found", 404),
        ("not_reportable", 409),
        ("full", 409),
        ("bad_order", 422),
        ("other_conversation", 409),
    ],
)
def test_api_maps_errors(client, code, status):
    client.fake("add_item", error=ReportError(code, "이유"))
    res = client.put("/reports/5/items/9")
    assert res.status_code == status and res.json()["detail"] == "이유"


def test_api_creates_and_generates_at_once(client):
    """대화창 [보고서]: 고른 답으로 만들고 문장 생성까지 한 번에"""
    client.fake("generate", {**READ, "status": "ready", "content": {"summary": "s"}})
    res = client.post(
        "/reports", json={"turn_ids": [1, 2], "generate": True, "chart_views": {"2": "bar"}}
    )
    assert res.status_code == 201 and res.json()["status"] == "ready"
    assert [name for name, _ in client.calls] == ["create", "generate"]
    assert client.calls[0][1][-1] == {2: "bar"}
    assert client.calls[1][1] == (3, 5)  # 만든 보고서(id 5)를 그 사용자로 생성
    client.calls.clear()
    assert client.post("/reports", json={"turn_ids": [1]}).json()["status"] == "draft"
    assert [name for name, _ in client.calls] == ["create"]


def test_api_missing_report_is_404(client):
    assert client.get("/reports/5").status_code == 404
    assert client.delete("/reports/5").status_code == 404


def test_api_login_required_and_inputs_checked(client):
    app.dependency_overrides.pop(get_current_user)
    assert client.get("/reports").status_code == 401
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=3)
    assert client.put("/reports/0/items/9").status_code == 422
    assert client.post("/reports", json={"turn_ids": list(range(1, 20))}).status_code == 422
    assert client.patch("/reports/5", json={"title": ""}).status_code == 422


# ---- 생성 (문장 쓰기는 흉내) ---------------------------------------------------------


def test_generate_saves_content_and_status(db, monkeypatch):
    from app.services import report_writer

    seen = []

    async def write(title, purpose, items, style=None):
        seen.append((title, purpose, [i["turn_id"] for i in items]))
        return {"summary": [{"text": "요약", "refs": ["r1"]}], "generated_by": "hcx"}

    monkeypatch.setattr(report_writer, "write", write)
    rep = run(r.create(db.a, "제목", "목적", [db.t["ok1"], db.t["ok2"]]))
    rep = run(r.generate(db.a, rep["id"]))
    assert rep["status"] == "ready" and rep["content"]["summary"][0]["text"] == "요약"
    assert seen == [("제목", "목적", [db.t["ok1"], db.t["ok2"]])]
    rep = run(r.remove_item(db.a, rep["id"], db.t["ok2"]))
    assert rep["status"] == "draft" and rep["content"] is None


def test_generate_empty_and_others_report(db):
    rep = run(r.create(db.a))
    with pytest.raises(ReportError) as e:
        run(r.generate(db.a, rep["id"]))
    assert e.value.code == "empty"
    with pytest.raises(ReportError) as e:
        run(r.generate(db.b, rep["id"]))
    assert e.value.code == "not_found"


def test_shorten_only_saves_a_strictly_shorter_body(db, monkeypatch):
    from app.services import report_writer

    texts = iter(["처음 요약", "조금 더 길어진 보고서 요약", "처음 요약", "요약"])
    calls = []

    async def write(title, purpose, items, style=None, *, compact=False):
        calls.append(compact)
        return {"summary": [{"text": next(texts), "refs": ["r1"]}]}

    monkeypatch.setattr(report_writer, "write", write)
    draft = run(r.create(db.a, turn_ids=[db.t["ok1"]]))
    original = run(r.generate(db.a, draft["id"]))
    for _ in range(2):
        with pytest.raises(ReportError) as e:
            run(r.generate(db.a, draft["id"], "짧게", shorten=True))
        assert e.value.code == r.NOT_SHORTER
        saved = run(r.detail(db.a, draft["id"]))
        assert saved["content"] == original["content"]
        assert saved["updated_at"] == original["updated_at"]

    saved = run(r.generate(db.a, draft["id"], "짧게", shorten=True))
    assert saved["status"] == "ready" and saved["content"]["summary"][0]["text"] == "요약"
    assert saved["updated_at"] > original["updated_at"]
    assert calls == [False, True, True, True]


def test_generate_discards_when_items_changed_meanwhile(db, monkeypatch):
    from app.services import report_writer

    rep = run(r.create(db.a, None, None, [db.t["ok1"]]))

    async def write(title, purpose, items, style=None):
        await r.add_item(db.a, rep["id"], db.t["ok2"])  # 만드는 동안 다른 탭에서 담음
        return {"summary": []}

    monkeypatch.setattr(report_writer, "write", write)
    with pytest.raises(ReportError) as e:
        run(r.generate(db.a, rep["id"]))
    assert e.value.code == "changed"
    assert run(r.detail(db.a, rep["id"]))["content"] is None


def test_api_generate_timeout_and_errors(client, monkeypatch):
    from app.core.config import get_settings

    async def slow(*args):
        await asyncio.sleep(1)

    monkeypatch.setattr(r, "generate", slow)
    monkeypatch.setattr(get_settings(), "report_timeout", 0.01)
    assert client.post("/reports/5/generate").status_code == 504
    client.fake("generate", error=ReportError("empty", "없음"))
    assert client.post("/reports/5/generate").status_code == 409
    client.fake("generate")
    assert client.post("/reports/5/generate").status_code == 200


def test_generate_discards_when_title_changed_meanwhile(db, monkeypatch):
    """만드는 동안 제목 · 목적을 바꾸면 이전 내용으로 만든 보고서를 ready 로 저장하지 않는다"""
    from app.services import report_writer

    rep = run(r.create(db.a, "처음 제목", None, [db.t["ok1"]]))

    async def write(title, purpose, items, style=None):
        await r.update(db.a, rep["id"], title="바꾼 제목")
        return {"summary": []}

    monkeypatch.setattr(report_writer, "write", write)
    with pytest.raises(ReportError) as e:
        run(r.generate(db.a, rep["id"]))
    assert e.value.code == "changed"
    after = run(r.detail(db.a, rep["id"]))
    assert after["status"] == "draft" and after["content"] is None and after["title"] == "바꾼 제목"
