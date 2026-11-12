"""수치 대화 이어 가기(ask_session): 조건 고치기, 이어받기, 되묻기 답, 차트·요약, 한 턴 흐름
(HCX·계산·DB 없이: 변경 추출과 답 만들기, 저장 함수를 바꿔 끼운다)
"""

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.services import ask_session as s
from app.services import indicators, qa

BASE = {
    "type": "trend",
    "indicator": "출생아 수",
    "regions": ["부산"],
    "year": 2024,
    "start_year": 2020,
    "end_year": 2024,
    "sex": "계",
    "level": "시도",
    "top": None,
    "ascending": False,
}
USER = {"indicator": "user", "regions": "user", "period": "user", "sex": "default"}


@pytest.fixture(autouse=True)
def names(monkeypatch: pytest.MonkeyPatch) -> None:
    known = {"출생아 수", "사망자 수", "합계출산율", "고령인구 비율"}
    monkeypatch.setattr(indicators, "canonical_name", lambda n: n if n in known else None)
    monkeypatch.setattr(
        indicators, "find_in_text", lambda t: next((n for n in known if n in t), None)
    )
    monkeypatch.setattr(indicators, "specs", lambda: dict.fromkeys(known, {}))


# ---- 조건 고치기 ---------------------------------------------------------------------------


def test_add_region_turns_trend_into_compare() -> None:
    c, changed = s.apply_changes(BASE, {"regions": {"op": "add", "value": "서울"}})
    assert c["regions"] == ["부산", "서울"] and c["type"] == "compare" and changed == {"regions"}
    assert BASE["regions"] == ["부산"]  # 원래 조건은 건드리지 않는다
    again, _ = s.apply_changes(c, {"regions": {"op": "add", "value": ["서울특별시"]}})
    assert again["regions"] == ["부산", "서울"]  # 이미 있는 지역은 또 넣지 않는다


def test_set_and_remove_region() -> None:
    c, _ = s.apply_changes(BASE, {"regions": {"op": "set", "value": ["대구"]}})
    assert c["regions"] == ["대구"] and c["type"] == "trend"  # 지역 하나 + 기간 → 추이 그대로
    two, _ = s.apply_changes(BASE, {"regions": {"op": "add", "value": ["서울"]}})
    back, _ = s.apply_changes(two, {"regions": {"op": "remove", "value": ["부산광역시"]}})
    assert back["regions"] == ["서울"] and back["type"] == "trend"


def test_single_year_turns_trend_into_value_and_span_back() -> None:
    one, changed = s.apply_changes(BASE, {"year": 2022})
    assert (one["type"], one["year"], one["start_year"], one["end_year"]) == (
        "value",
        2022,
        None,
        None,
    )
    assert changed == {"period"}
    span, _ = s.apply_changes(one, {"start_year": 2015})
    assert (span["type"], span["start_year"], span["end_year"]) == ("trend", 2015, 2022)


def test_rank_keeps_type_for_order_and_year_but_not_for_region() -> None:
    rank = {**BASE, "type": "rank", "regions": [], "start_year": None, "end_year": None, "top": 5}
    c, changed = s.apply_changes(rank, {"ascending": True, "top": 3})
    assert (c["type"], c["ascending"], c["top"]) == ("rank", True, 3) and changed == {"view"}
    assert s.apply_changes(rank, {"year": 2020})[0]["type"] == "rank"
    there, _ = s.apply_changes(rank, {"regions": {"op": "set", "value": ["세종"]}})
    assert there["type"] == "value"  # "거기는?" → 그 지역의 값


def test_explicit_type_and_indicator_change() -> None:
    c, changed = s.apply_changes(
        {**BASE, "ages": [[20, 29]]}, {"indicator": "사망자 수", "type": "rank"}
    )
    assert c["indicator"] == "사망자 수" and c["type"] == "rank" and "ages" not in c
    assert c["regions"] == ["부산"] and changed == {"indicator", "view"}


def test_bad_values_are_ignored() -> None:
    bad = {
        "sex": "모름",
        "year": "작년",
        "top": -1,
        "type": "pie",
        "regions": {"op": "add", "value": []},
    }
    c, changed = s.apply_changes(BASE, bad)
    assert c == BASE and changed == set()


# ---- 출처와 이어받기 -----------------------------------------------------------------------


def test_sources_of_question() -> None:
    parsed = {**BASE, "sex": "계"}
    assert s.sources_of("최근 5년 부산 출생아 수", parsed)["period"] == "user"
    assert s.sources_of("부산 출생아 수", parsed)["period"] == "default"
    assert s.sources_of("전국 합계출산율", {**parsed, "regions": []})["regions"] == "default"
    assert s.sources_of("부산 여자 인구", {**parsed, "sex": "여"})["sex"] == "user"


def test_new_question_inherits_region_and_same_kind_period() -> None:
    new = {**BASE, "indicator": "사망자 수", "regions": [], "start_year": 2021, "end_year": 2025}
    plain = {"indicator": "user", "regions": "default", "period": "default", "sex": "default"}
    p, src = s.inherit(new, plain, BASE, USER)
    assert p["regions"] == ["부산"] and (p["start_year"], p["end_year"]) == (2020, 2024)
    assert src["regions"] == "inherited" and src["period"] == "inherited"
    assert (
        s.inherited_notice(p, src) == "앞 질문의 조건을 이어받았습니다: 지역 부산, 기간 2020~2024년"
    )

    value = {**new, "type": "value", "year": 2025, "start_year": None, "end_year": None}
    p, src = s.inherit(value, plain, BASE, USER)  # 추이를 보다 값 하나를 물음 → 기간은 최신 그대로
    assert p["regions"] == ["부산"] and p["year"] == 2025 and src["period"] == "default"


def test_no_inherit_for_said_conditions_rank_or_default_period() -> None:
    said = {**BASE, "regions": ["서울"]}
    src = {"indicator": "user", "regions": "user", "period": "user", "sex": "default"}
    assert s.inherit(said, src, BASE, USER)[0]["regions"] == ["서울"]
    rank = {**BASE, "type": "rank", "regions": []}
    plain = {"indicator": "user", "regions": "default", "period": "default", "sex": "default"}
    assert s.inherit(rank, plain, BASE, USER)[0]["regions"] == []  # 순위는 지역 없이 묻는다
    prev_default = {**USER, "period": "default"}
    assert s.inherit({**BASE, "regions": []}, plain, BASE, prev_default)[1]["period"] == "default"


def test_conditions_keep_the_period_actually_answered() -> None:
    """ "최근 5년"은 계산할 때 자료의 마지막 연도로 정해진다 → 조건에는 실제 답한 기간을 적는다"""
    parsed = {
        **BASE,
        "start_year": 2019,
        "end_year": 2023,
        "asked": {"recent": 5},
        "corrections": [],
    }
    series = [{"year": y, "value": 1} for y in range(2021, 2026)]
    c = s.effective(parsed, {"status": "ok", "series": series})
    assert (c["start_year"], c["end_year"], c["year"]) == (2021, 2025, 2025)
    assert "asked" not in c and "corrections" not in c
    assert s.effective({**BASE, "type": "value"}, {"status": "ok", "year": 2023})["year"] == 2023
    failed = s.effective(parsed, {"status": "out_of_range"})
    assert failed["start_year"] == 2019 and failed["asked"] == {
        "recent": 5
    }  # 답하지 못했으면 그대로


def test_applied_shows_conditions_with_source() -> None:
    rows = s.applied(BASE, {**USER, "period": "inherited"})
    assert rows == [
        {"name": "지표", "value": "출생아 수", "source": "user"},
        {"name": "지역", "value": "부산", "source": "user"},
        {"name": "기간", "value": "2020~2024년", "source": "inherited"},
        {"name": "성별", "value": "계", "source": "default"},
    ]
    assert s.applied({"type": "other"}, {}) == []


# ---- 되묻기 답, 규칙으로 알아보는 요청 ------------------------------------------------------


def test_pick_region_reply() -> None:
    options = ["광주광역시", "경기도 광주시"]
    assert s.pick_region("광주광역시요", options) == "광주광역시"
    assert s.pick_region("경기도 광주시", options) == "경기도 광주시"
    assert s.pick_region("2번", options) == "경기도 광주시"
    assert s.pick_region("광주", options) is None  # 여전히 둘 다 맞는다
    assert s.pick_region("서울 인구 알려줘", options) is None
    c = s.replace_region({**BASE, "regions": ["서울", "광주"]}, "광주", "광주광역시")
    assert c["regions"] == ["서울", "광주광역시"]


@pytest.mark.parametrize(
    ("question", "view"),
    [
        ("막대그래프로 보여줘", "bar"),
        ("표로", "table"),
        ("꺾은선으로 바꿔줘", "line"),
        ("부산 출생아 수 막대그래프로", None),  # 지표를 함께 말하면 새 질문일 수 있다
        ("2020년 막대그래프로 보여줘", None),
        ("막대한 영향이 있나요", None),
    ],
)
def test_chart_request(question, view) -> None:
    assert s.chart_request(question) == view


def test_chart_view_and_summary() -> None:
    line = {"type": "line", "rows": []}
    assert s.chart_with_view(line, "bar")["view"] == "bar"
    assert s.chart_with_view(line, "table")["view"] == "table"
    assert s.chart_with_view({"type": "parts", "rows": []}, "line") is None
    assert s.chart_with_view(None, "bar") is None
    assert s.summary_request("요약해줘") and s.summary_request("한 줄로 정리해 줘")
    assert not s.summary_request("서울 인구 요약 통계 알려줘")
    assert s.lead("핵심 문장입니다.\n\n- 2020년: 1\n출처: ...") == "핵심 문장입니다."


def test_parse_delta_falls_back_to_new() -> None:
    assert s.parse_delta("모르겠습니다")["request_type"] == "new"
    assert s.parse_delta('{"request_type": "change", "changes": {}}')["request_type"] == "new"
    ok = s.parse_delta('```json\n{"request_type": "change", "changes": {"sex": "남"}}\n```')
    assert ok["request_type"] == "change" and ok["changes"] == {"sex": "남"}
    unclear = s.parse_delta('{"request_type": "change", "changes": {}, "unclear": "무엇을?"}')
    assert unclear["unclear"] == "무엇을?"
    assert s.parse_delta('{"request_type": "chart", "chart": "pie"}')["chart"] is None


# ---- 한 턴 흐름 ----------------------------------------------------------------------------


class Talk:
    """HCX 변경 추출과 답 만들기를 대신하고, 세션은 메모리에 둔다"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store: dict = {}
        self.turns: list = []
        self.asked: list = []  # 새 질문으로 해석한 말 (HCX 해석 호출)
        self.extracted: list = []  # 변경 추출을 부른 말 (HCX 호출)
        self.answered: list = []  # 조건으로 바로 답한 것 (HCX 없음)
        self.delta: dict = {"request_type": "new", "changes": {}, "chart": None, "unclear": None}
        self.parsed: dict = dict(BASE)
        self.fail_when: object = lambda parsed: False
        self.at = s.now()
        self.sid: str | None = None
        monkeypatch.setattr(qa, "ask", self._ask)
        monkeypatch.setattr(qa, "answer", self._answer)
        monkeypatch.setattr(s, "extract", self._extract)
        monkeypatch.setattr(s, "load", self._load)
        monkeypatch.setattr(s, "save", self._save)
        monkeypatch.setattr(s, "log_turn", self._log)
        monkeypatch.setattr(s, "now", lambda: self.at)

    def _result(self, parsed: dict, question: str = "") -> dict:
        regions = parsed.get("regions") or []
        if "과거" in question:
            result = {"status": "missing_year"}
        elif "광주" in regions:
            result = {"status": "ambiguous_region", "region": "광주"}
            result["candidates"] = ["광주광역시", "경기도 광주시"]
        elif self.fail_when(parsed):
            result = {"status": "out_of_range"}
        elif parsed.get("type") == "trend":
            years = range(parsed["start_year"], parsed["end_year"] + 1)
            result = {"status": "ok", "series": [{"year": y, "value": y - 2000} for y in years]}
            result.update(indicator=parsed["indicator"], region=regions[0], unit="명")
        else:
            result = {"status": "ok", "value": 1}
        text = f"{parsed.get('type')} {regions} {s.period_text(parsed)}"
        return {
            "status": result["status"],
            "answer": f"{text}\n\n- 수치 목록",
            "parsed": parsed,
            "result": result,
            "sources": [{"name": "출처", "basis": "기준", "tbl_id": "T", "url": None}],
            "trace": {},
        }

    async def _ask(self, question, adjust=None):
        self.asked.append(question)
        parsed = dict(self.parsed)
        if adjust:
            parsed = adjust(parsed)
        return self._result(parsed, question)

    async def _answer(self, parsed, trace=None, question=""):
        self.answered.append(parsed)
        return self._result(parsed)

    async def _extract(self, state, question, trace):
        self.extracted.append(question)
        return {"request_type": "new", "changes": {}, "chart": None, "unclear": None, **self.delta}

    async def _load(self, session_id, at):
        row = self.store.get(session_id)
        if row is None:
            return None, False
        return (None, True) if at - row[1] > s.SESSION_FOR else (row[0], False)

    async def _save(self, session_id, state):
        self.store[session_id] = (state, self.at)

    async def _log(self, session_id, user_id, turn_no, question, res, before, after):
        self.turns.append((turn_no, res.get("request_type"), question))

    def say(self, question: str, **delta) -> dict:
        self.delta = delta
        res = asyncio.run(s.handle(self.sid, question, user_id=7))
        self.sid = res["session_id"]
        return res


@pytest.fixture
def talk(monkeypatch: pytest.MonkeyPatch) -> Talk:
    return Talk(monkeypatch)


def test_first_question_starts_a_session(talk: Talk) -> None:
    res = talk.say("부산 출생아 최근 5년 보여 줘")
    assert (
        res["status"] == "ok"
        and res["request_type"] == "new"
        and res["session_id"].startswith("A_")
    )
    assert res["chart"]["type"] == "line" and res["notice"] is None
    assert [a["value"] for a in res["applied"]] == ["출생아 수", "부산", "2020~2024년", "계"]
    assert talk.extracted == []  # 첫 질문에는 변경 추출을 부르지 않는다
    assert talk.turns == [(1, "new", "부산 출생아 최근 5년 보여 줘")]


def test_change_recomputes_without_reparsing(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["request_type"] == "change" and res["parsed"]["type"] == "value"
    assert res["parsed"]["year"] == 2022 and res["parsed"]["regions"] == ["부산"]
    assert len(talk.asked) == 1 and len(talk.answered) == 1  # 질문 해석(HCX)은 처음 한 번뿐
    period = next(a for a in res["applied"] if a["name"] == "기간")
    assert period == {"name": "기간", "value": "2022년", "source": "user"}
    res = talk.say(
        "서울도 같이", request_type="change", changes={"regions": {"op": "add", "value": "서울"}}
    )
    assert res["parsed"]["regions"] == ["부산", "서울"] and res["parsed"]["type"] == "compare"
    assert "2022년 기준으로 비교" not in (
        res["notice"] or ""
    )  # 이미 한 해를 보던 중이면 알리지 않는다


def test_adding_region_to_trend_tells_the_period_was_narrowed(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    change = {"regions": {"op": "add", "value": "서울"}}
    res = talk.say("서울도 같이", request_type="change", changes=change)
    assert res["parsed"]["type"] == "compare" and "2024년 기준으로 비교했습니다" in res["notice"]


def test_chart_and_summary_reuse_last_answer(talk: Talk) -> None:
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("막대그래프로 보여줘")
    assert res["request_type"] == "chart" and res["chart"]["view"] == "bar"
    assert res["chart"]["rows"] == first["chart"]["rows"] and res["sources"] == first["sources"]
    assert talk.extracted == [] and talk.answered == []  # HCX도 계산도 부르지 않는다
    res = talk.say("요약해줘")
    assert res["request_type"] == "summary" and res["answer"] == "trend ['부산'] 2020~2024년"
    # 조건은 그대로라 다음 변경이 이어진다
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["parsed"]["regions"] == ["부산"]
    assert [t[1] for t in talk.turns] == ["new", "chart", "summary", "change"]


def test_chart_that_cannot_be_drawn_is_asked_back(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "start_year": None, "end_year": None}
    talk.say("2024년 부산 출생아 수")  # 값 하나라 차트가 없다
    res = talk.say("막대그래프로 보여줘")
    assert res["status"] == "unclear" and res["chart"] is None
    assert talk.say("2022년만", request_type="change", changes={"year": 2022})["status"] == "ok"


def test_new_question_inherits_and_tells(talk: Talk) -> None:
    talk.say("부산 출생아 2020~2024년 보여 줘")
    talk.parsed = {
        **BASE,
        "indicator": "사망자 수",
        "regions": [],
        "start_year": 2021,
        "end_year": 2025,
    }
    res = talk.say("사망자 수 추이는?", request_type="new")
    assert res["request_type"] == "new" and res["parsed"]["regions"] == ["부산"]
    assert (res["parsed"]["start_year"], res["parsed"]["end_year"]) == (2020, 2024)
    assert res["notice"] == "앞 질문의 조건을 이어받았습니다: 지역 부산, 기간 2020~2024년"
    assert {a["name"]: a["source"] for a in res["applied"]}["지역"] == "inherited"


def test_inherited_period_is_dropped_when_it_has_no_data(talk: Talk) -> None:
    talk.say("부산 출생아 2020~2024년 보여 줘")
    talk.parsed = {
        **BASE,
        "indicator": "사망자 수",
        "regions": [],
        "start_year": 2021,
        "end_year": 2025,
    }
    talk.fail_when = lambda parsed: parsed.get("start_year") == 2020
    res = talk.say("사망자 수 추이는?", request_type="new")
    assert res["status"] == "ok" and res["parsed"]["start_year"] == 2021
    assert res["parsed"]["regions"] == ["부산"]  # 지역은 이어받은 그대로
    assert "기본 기간으로 답했습니다" in res["notice"] and "지역 부산" in res["notice"]


def test_year_reply_is_merged_into_asked_question(talk: Talk) -> None:
    first = talk.say("과거와 최근 인구수 비교해줘")
    assert first["status"] == "missing_year"
    res = talk.say("2000년")
    assert res["request_type"] == "reply" and res["question"] == "2000년과 최근 인구수 비교해줘"
    assert res["status"] == "ok" and talk.extracted == []
    # 되묻는 중에 다른 질문이 오면 되묻기는 버린다
    talk.say("과거와 최근 인구수 비교해줘")
    res = talk.say("2024년 서울 고령인구 비율 알려줘", request_type="new")
    assert res["request_type"] == "new" and res["question"] == "2024년 서울 고령인구 비율 알려줘"


def test_region_reply_replaces_only_the_asked_region(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "compare", "regions": ["서울", "광주"]}
    first = talk.say("2024년 서울 광주 출생아 수 비교")
    assert first["status"] == "ambiguous_region"
    res = talk.say("광주광역시요")
    assert res["request_type"] == "reply" and res["parsed"]["regions"] == ["서울", "광주광역시"]
    assert talk.extracted == [] and len(talk.asked) == 1  # HCX를 다시 부르지 않는다


def test_unclear_keeps_conditions(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("그거 말고", request_type="change", unclear="무엇을 바꿀지 알 수 없습니다.")
    assert res["status"] == "unclear" and "무엇을 바꿀지" in res["answer"]
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["parsed"]["regions"] == ["부산"] and res["parsed"]["year"] == 2022


def test_expired_session_starts_over_and_tells(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    talk.at += s.SESSION_FOR + timedelta(minutes=1)
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["request_type"] == "new" and "초기화됐습니다" in res["notice"]
    assert talk.extracted == []  # 이어 갈 조건이 없어 새 질문으로 해석한다


def test_non_statistics_answer_is_not_continued(talk: Talk) -> None:
    talk.parsed = {"type": "other"}
    res = talk.say("안녕")
    assert res["applied"] == []
    talk.parsed = dict(BASE)
    assert talk.say("부산 출생아 최근 5년")["request_type"] == "new" and talk.extracted == []


def test_storage_failure_still_answers(talk: Talk, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.exc import OperationalError

    async def broken(*args, **kwargs):
        raise OperationalError("select", {}, Exception("DB 없음"))

    monkeypatch.setattr(s, "load", broken)
    monkeypatch.setattr(s, "save", broken)
    res = talk.say("부산 출생아 최근 5년 보여 줘")
    assert res["status"] == "ok" and res["session_id"]


def test_api_returns_session_fields(talk: Talk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qa, "log_request", lambda *a, **k: None)
    monkeypatch.setattr(qa, "log_missing", lambda *a, **k: None)
    client = TestClient(main.app)
    first = client.post("/ask", json={"question": "부산 출생아 최근 5년 보여 줘"}).json()
    assert first["request_type"] == "new" and first["applied"][1]["value"] == "부산"
    body = {"question": "표로 보여줘", "session_id": first["session_id"]}
    second = client.post("/ask", json=body).json()
    assert second["request_type"] == "chart" and second["chart"]["view"] == "table"
    assert second["session_id"] == first["session_id"]
