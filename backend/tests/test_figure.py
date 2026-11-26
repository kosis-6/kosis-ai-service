"""figure: result의 여러 모양 → 대표 수치 한 형식, reportable 조건 (명세서 2장)"""

import pytest

from app.schemas.ask import AskResponse
from app.services import figure

SRC = {"tbl_id": "DT_TEST", "source": "통계청, 시험표 (DT_TEST)", "basis": "매년 11월 1일"}


def ok(**kw):
    return {"status": "ok", "indicator": "1인가구", "unit": "가구", **SRC, **kw}


def test_value():
    f = figure.figure_of(ok(region="서울특별시", year=2020, sex="계", value=1390701))
    assert f["kind"] == "value"
    assert f["value"] == 1390701
    assert f["unit"] == "가구"
    assert f["label"] == "2020년 서울특별시 1인가구"
    assert f["conditions"] == {
        "region": "서울특별시",
        "period": "2020년",
        "sex": None,
        "ages": None,
        "population": None,
    }
    assert f["tbl_ids"] == ["DT_TEST"]
    assert f["is_projection"] is False


def test_ratio_with_operands():
    r = ok(
        region="서울특별시",
        year=2020,
        sex="여",
        kind="ratio",
        unit="%",
        value=53.19,
        formula="여성 1인가구 ÷ 1인가구 × 100",
        operands=[
            {"label": "여성 1인가구", "value": 739702, "unit": "가구"},
            {"label": "1인가구", "value": 1390701, "unit": "가구"},
        ],
    )
    f = figure.figure_of(r)
    assert f["kind"] == "ratio"
    assert f["conditions"]["sex"] == "여"
    assert [o["value"] for o in f["operands"]] == [739702, 1390701]
    assert f["operands"][0]["tbl_id"] == "DT_TEST"
    assert f["formula"].startswith("여성")


@pytest.mark.parametrize(("op", "kind"), [("−", "diff"), ("+", "sum")])
def test_cross_diff_and_sum(op, kind):
    r = ok(
        region="전국",
        year=2024,
        value=1,
        op=op,
        operands=[{"label": "a", "value": 1, "unit": "명"}],
    )
    assert figure.figure_of(r)["kind"] == kind


def test_series():
    r = ok(
        region="서울특별시",
        series=[{"year": 2015, "value": 1}, {"year": 2020, "value": 2}, {"year": 2025, "value": 3}],
        change={"from_year": 2015, "to_year": 2025, "diff": 2},
    )
    f = figure.figure_of(r)
    assert f["kind"] == "series"
    assert f["value"] == 3
    assert f["conditions"]["period"] == "2015년~2025년"
    assert [x["label"] for x in f["rows"]] == ["2015년", "2020년", "2025년"]
    assert f["change"]["diff"] == 2


def test_compare_has_rows_not_value():
    r = ok(year=2025, values=[{"region": "서울", "value": 2}, {"region": "부산", "value": 1}])
    f = figure.figure_of(r)
    assert f["kind"] == "compare"
    assert f["value"] is None
    assert f["conditions"]["region"] == "서울, 부산"


def test_ranking_with_target():
    r = ok(
        year=2025,
        level="시도",
        ranking=[
            {"rank": 1, "region": "경기", "value": 9},
            {"rank": 2, "region": "서울", "value": 8},
        ],
        target={"region": "서울", "rank": 2, "value": 8},
    )
    f = figure.figure_of(r)
    assert f["kind"] == "ranking"
    assert f["value"] == 8
    assert f["rows"][0] == {"label": "경기", "value": 9, "rank": 1}
    assert f["conditions"]["region"] == "시도"


def test_parts_use_share_and_count():
    r = ok(
        region="서울",
        year=2020,
        subject="1인가구 연령",
        parts=[{"label": "70세 이상", "count": 5, "share": 13.3}],
    )
    f = figure.figure_of(r)
    assert f["kind"] == "parts"
    assert f["unit"] == "%"
    assert f["rows"] == [{"label": "70세 이상", "value": 13.3, "count": 5}]
    assert f["indicator"] == "1인가구 연령"


def test_month_sum_members():
    r = ok(
        region="서울특별시",
        year=2024,
        value=10,
        period_label="2024년 1~3월",
        members=[
            {"region": "1월", "value": 3},
            {"region": "2월", "value": 3},
            {"region": "3월", "value": 4},
        ],
    )
    f = figure.figure_of(r)
    assert f["kind"] == "sum"
    assert f["conditions"]["period"] == "2024년 1~3월"
    assert len(f["rows"]) == 3


@pytest.mark.parametrize(
    "r",
    [
        {"status": "missing_data", "reason": "없음"},
        {"status": "ok", "split": [ok(value=1), ok(value=2)]},
        {"status": "ok"},
        None,
    ],
)
def test_no_figure(r):
    assert figure.figure_of(r) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"scenario": "중위"},
        {"future_after": 2025},
        {"answer_note": "장래추계에 따른 예상값으로, 실제 통계가 아닙니다."},
        {"basis": "장래가구추계 기준"},
    ],
)
def test_projection(extra):
    assert figure.figure_of(ok(region="서울", year=2040, value=1, **extra))["is_projection"]


def test_ages_from_parsed():
    f = figure.figure_of(ok(region="서울", year=2020, value=1), {"ages": [[70, None], [20, 29]]})
    assert f["conditions"]["ages"] == "70세 이상, 20~29세"


def test_extra_sources_in_tbl_ids():
    r = ok(
        region="서울", year=2020, value=1, extra_sources=[{"tbl_id": "DT_B"}, {"tbl_id": "DT_TEST"}]
    )
    assert figure.figure_of(r)["tbl_ids"] == ["DT_TEST", "DT_B"]


def res_of(result, **kw):
    res = {"status": "ok", "result": result, **kw}
    res["figure"] = figure.figure_of(result)
    return res


def test_reportable_plain():
    assert figure.reportable(res_of(ok(region="서울", year=2020, value=1)))


@pytest.mark.parametrize(
    ("result", "kw"),
    [
        (ok(region="서울", year=2020, value=1, reused=True), {}),
        (ok(region="서울", year=2020, value=1), {"also_missing": "1인 가구 수"}),
        (ok(region="서울", year=2020, value=1), {"status": "tables"}),
        (ok(region="서울", year=2020, value=None), {}),
        ({**ok(region="서울", year=2020, value=1), "tbl_id": None}, {}),
        (
            ok(
                year=2020,
                values=[{"region": "서울", "value": 1}, {"region": "부산", "value": None}],
            ),
            {},
        ),
        (
            ok(
                region="서울",
                year=2020,
                value=1,
                operands=[{"label": "a", "value": None, "unit": "명"}],
            ),
            {},
        ),
    ],
)
def test_not_reportable(result, kw):
    assert not figure.reportable(res_of(result, **kw))


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (None, True),
        ({"first": {"verdict": "ok"}}, True),
        ({"first": {"verdict": "retry"}}, False),
        ({"first": {"verdict": "refuse"}}, False),
        ({"first": {"verdict": "retry"}, "replaced": {"attempt": 1}}, True),
    ],
)
def test_reportable_follows_judge(record, expected):
    trace = {"answer_judge": record} if record else {}
    res = res_of(ok(region="서울", year=2020, value=1), trace=trace)
    assert figure.reportable(res) is expected


def test_response_schema_accepts_figure():
    res = res_of(
        ok(
            region="서울",
            year=2020,
            kind="ratio",
            unit="%",
            value=53.19,
            operands=[
                {"label": "a", "value": 1, "unit": "가구"},
                {"label": "b", "value": 2, "unit": "가구"},
            ],
        )
    )
    body = AskResponse(answer="", parsed={}, sources=[], **res, reportable=True, turn_id=7)
    assert body.figure.kind == "ratio"
    assert body.figure.operands[1].value == 2
    assert body.turn_id == 7


def test_attach_figure_never_raises(monkeypatch):
    from app.services import ask_session

    def boom(*_):
        raise RuntimeError("x")

    monkeypatch.setattr(figure, "figure_of", boom)
    res = {"status": "ok", "result": ok(value=1)}
    ask_session._attach_figure(res)
    assert res["figure"] is None and res["reportable"] is False


def test_operands_keep_their_own_tables():
    """두 표를 엮은 계산: 분모의 출처가 첫 표로 바뀌지 않는다 (#244 리뷰)"""
    r = ok(
        region="서울",
        year=2020,
        kind="ratio",
        unit="%",
        value=1.0,
        operands=[
            {"label": "a", "value": 1, "unit": "명", "tbl_id": "DT_NUM"},
            {"label": "b", "value": 2, "unit": "명", "tbl_id": "DT_DEN"},
        ],
    )
    assert [o["tbl_id"] for o in figure.figure_of(r)["operands"]] == ["DT_NUM", "DT_DEN"]


def test_operand_tables_for_dictionary_indicators():
    from app.services import indicators

    assert indicators.OPERAND_TABLES["area"] == indicators.AREA_TABLE
    assert indicators.OPERAND_TABLES["households"] == indicators.HH_TABLE
    assert indicators.OPERAND_TABLES["foreigners"] == indicators.FOREIGN_TABLE


def test_replayed_conversation_keeps_turn_id():
    """지난 대화를 다시 열어도 담을 수 있는 답에는 turn_id 가 있다 (#244 리뷰)"""
    import asyncio
    import json
    import uuid

    from sqlalchemy import text

    from app.services import conversations

    async def go():
        from app.core.db import engine

        tag = uuid.uuid4().hex[:10]
        sid = f"fig-test-{tag}"
        try:
            async with engine.begin() as conn:
                uid = (
                    await conn.execute(
                        text(
                            "insert into app.users (email, password_hash)"
                            " values (:e, 'x') returning id"
                        ),
                        {"e": f"fig-test-{tag}@example.com"},
                    )
                ).scalar_one()
                await conn.execute(
                    text(
                        "insert into app.conversations (session_id, user_id, title)"
                        " values (:s, :u, 't')"
                    ),
                    {"s": sid, "u": uid},
                )
                ids = []
                for no, resp in enumerate([{"answer": "a", "reportable": True}, {"answer": "b"}]):
                    ids.append(
                        (
                            await conn.execute(
                                text(
                                    "insert into app.chat_turns (session_id, user_id, turn_no,"
                                    " message, recomputed, response) values (:s, :u, :n, 'q',"
                                    " true, cast(:r as jsonb)) returning id"
                                ),
                                {"s": sid, "u": uid, "n": no + 1, "r": json.dumps(resp)},
                            )
                        ).scalar_one()
                    )
            found = await conversations.detail(uid, sid)
            async with engine.begin() as conn:
                await conn.execute(
                    text("delete from app.chat_turns where user_id = :u"), {"u": uid}
                )
                await conn.execute(text("delete from app.users where id = :u"), {"u": uid})
            return ids, found
        finally:
            await engine.dispose()

    try:
        ids, found = asyncio.run(go())
    except Exception as e:  # DB 없음
        pytest.skip(f"app 스키마 DB 없음: {e}")
    responses = [t["response"] for t in found["turns"]]
    assert responses[0]["turn_id"] == ids[0]
    assert "turn_id" not in responses[1]  # 담을 수 없는 답에는 붙이지 않는다
