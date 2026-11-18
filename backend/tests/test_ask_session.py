"""수치 대화 이어 가기(ask_session): 조건 고치기, 이어받기, 되묻기 답, 차트·요약, 한 턴 흐름
(HCX·계산·DB 없이: 변경 추출과 답 만들기, 저장 함수를 바꿔 끼운다)
"""

import asyncio
import copy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import stats_db
from app.services import ask_session as s
from app.services import dynamic_indicator, indicators, qa, table_guide

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
    def no_db():
        pytest.fail("대화 단위 테스트에서 실제 통계 DB를 호출했습니다")

    def resolve_region(name):
        if name == "수원":
            return {"status": "ok", "display": "경기도 수원시"}
        if name == "고성":
            return {
                "status": "ambiguous_region",
                "candidates": ["강원특별자치도 고성군", "경상남도 고성군"],
            }
        if name in ("부산", "서울"):
            return {"status": "ok", "display": name, "kind": "시도"}
        return {"status": "unknown_region", "region": name}

    monkeypatch.setattr(stats_db, "_engine", no_db)
    monkeypatch.setattr(indicators, "resolve_region", resolve_region)
    known = {"출생아 수", "사망자 수", "합계출산율", "고령인구 비율"}
    monkeypatch.setattr(indicators, "canonical_name", lambda n: n if n in known else None)
    monkeypatch.setattr(
        indicators, "find_in_text", lambda t: next((n for n in known if n in t), None)
    )
    monkeypatch.setattr(indicators, "specs", lambda: dict.fromkeys(known, {}))
    monkeypatch.setattr(
        indicators, "find_regions", lambda t: [r for r in ("서울", "부산") if r in t]
    )


# ---- 조건 고치기 ---------------------------------------------------------------------------


def test_add_region_keeps_trend_for_each_region() -> None:
    c, changed = s.apply_changes(BASE, {"regions": {"op": "add", "value": "서울"}})
    assert c["regions"] == ["부산", "서울"] and c["type"] == "trend" and changed == {"regions"}
    assert (c["start_year"], c["end_year"]) == (2020, 2024)  # 기간은 그대로 (지역마다 추이)
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
    for said in (
        "요약",
        "정리해줘",
        "짧게 요약 좀",
        "한줄로 요약해줘",
        "요약 부탁해",
        "요약해 주세요.",
    ):
        assert s.summary_request(said), said
    assert not s.summary_request("서울 인구 요약 통계 알려줘")
    # 요약·정리로 시작해도 뒤에 다른 말이 오면 질문이다
    for said in ("요약 통계 보여줘", "정리해고 통계 알려줘", "요약해서 표로 보여줘"):
        assert not s.summary_request(said), said
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
        self.searched: list = []  # 통계표 찾기에 보낸 말 (session, selections)
        self.found: dict = {"status": "ok", "answer": "인구총조사에서 확인할 수 있습니다."}
        self.at = s.now()
        self.sid: str | None = None
        monkeypatch.setattr(qa, "ask", self._ask)
        monkeypatch.setattr(qa, "answer", self._answer)
        monkeypatch.setattr(s, "extract", self._extract)
        monkeypatch.setattr(s, "load", self._load)
        monkeypatch.setattr(s, "save", self._save)
        monkeypatch.setattr(s, "log_turn", self._log)
        monkeypatch.setattr(s, "now", lambda: self.at)
        monkeypatch.setattr(table_guide, "find_tables", self._find)
        monkeypatch.setattr(table_guide, "cards_for", self._cards)
        self.dynamic: dict | None = None  # 찾은 표로 바로 계산한 답 (None이면 표 안내)
        monkeypatch.setattr(dynamic_indicator, "answer", self._dynamic)
        self.cached: dict | None = None  # 계산 방법 캐시로 바로 계산한 답
        monkeypatch.setattr(dynamic_indicator, "cached_answer", self._cached)

    def _result(self, parsed: dict, question: str = "") -> dict:
        regions = parsed.get("regions") or []
        if parsed.get("indicator") == "반려동물 수":
            result = {"status": "unknown_indicator"}
        elif "과거" in question:
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
            first, last = years[0], years[-1]
            result["change"] = {
                "from_year": first,
                "to_year": last,
                "diff": last - first,
                "diff_unit": "명",
            }
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

    async def _find(self, question, session_id, selections, user_id):
        self.searched.append((question, session_id, selections))
        return {**self.found, "recommendations": [{"tbl_id": "DT_X"}], "clarify": None}

    async def _cached(self, question, parsed, trace):
        return copy.deepcopy(self.cached)

    async def _dynamic(self, question, parsed, found, trace):
        return copy.deepcopy(self.dynamic)

    async def _cards(self, question, sources):
        return [{"tbl_id": src["tbl_id"], "basis": src["basis"]} for src in sources]

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

    user_id: int | None = 7  # 말하는 사람. None 이면 손님

    def say(self, question: str, selections: dict | None = None, **delta) -> dict:
        self.delta = delta
        res = asyncio.run(s.handle(self.sid, question, user_id=self.user_id, selections=selections))
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


def test_adding_region_to_trend_keeps_the_period(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    change = {"regions": {"op": "add", "value": "서울"}}
    res = talk.say("서울도 같이", request_type="change", changes=change)
    assert res["parsed"]["type"] == "trend" and res["parsed"]["regions"] == ["부산", "서울"]
    assert (res["parsed"]["start_year"], res["parsed"]["end_year"]) == (2020, 2024)
    assert res["notice"] is None  # 기간을 줄이지 않으니 알릴 것도 없다


def test_chart_and_summary_reuse_last_answer(talk: Talk) -> None:
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("막대그래프로 보여줘")
    assert res["request_type"] == "chart" and res["chart"]["view"] == "bar"
    assert res["chart"]["rows"] == first["chart"]["rows"] and res["sources"] == first["sources"]
    assert talk.extracted == [] and talk.answered == []  # HCX도 계산도 부르지 않는다
    res = talk.say("요약해줘")
    # 답변 문장을 되풀이하지 않고 직전 계산 결과의 요약으로 답한다 (answer_summary.py)
    headline = "부산의 출생아 수는 2020~2024년에 4명 증가했습니다(20명 → 24명)."
    assert res["request_type"] == "summary" and res["summary"] == first["summary"]
    assert res["answer"].split("\n")[0] == headline == first["summary"]["headline"]
    assert "\n\n" not in res["answer"]  # 화면은 첫 빈 줄 앞까지만 말풍선에 쓴다
    assert talk.say("한 줄로 요약해줘")["answer"] == headline
    assert talk.extracted == [] and talk.answered == []  # 요약도 HCX·계산을 부르지 않는다
    # 조건은 그대로라 다음 변경이 이어진다
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["parsed"]["regions"] == ["부산"]
    assert [t[1] for t in talk.turns] == ["new", "chart", "summary", "summary", "change"]


def test_summary_of_an_answer_without_numbers_repeats_its_lead(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    talk.fail_when = lambda parsed: True
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["status"] == "out_of_range" and res["summary"] is None
    res = talk.say("요약해줘")
    assert res["request_type"] == "summary" and res["summary"] is None
    assert res["answer"] == "value ['부산'] 2022년"


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
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    talk.at += s.SESSION_FOR + timedelta(minutes=1)
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["request_type"] == "new" and "초기화됐습니다" in res["notice"]
    assert talk.extracted == []  # 이어 갈 조건이 없어 새 질문으로 해석한다
    assert res["session_id"] != first["session_id"]


@pytest.mark.parametrize("other", [8, None])
@pytest.mark.parametrize("deleted", [False, True])
def test_expired_or_deleted_session_does_not_mix_accounts(talk: Talk, other, deleted):
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    original = talk.store[first["session_id"]]
    if deleted:
        talk.store.pop(first["session_id"])
    else:
        talk.at += s.SESSION_FOR + timedelta(seconds=1)
    talk.user_id = other
    res = talk.say("새 질문")
    assert res["session_id"] != first["session_id"]
    assert talk.store[res["session_id"]][0]["user_id"] == other
    if not deleted:
        assert talk.store[first["session_id"]] == original


# ---- 대화의 주인: 대화 id 를 알아도 남의 대화를 이어 갈 수 없다 ------------------------------


@pytest.mark.parametrize("other", [7, 8, None])
def test_session_without_owner_metadata_starts_a_new_conversation(talk: Talk, other):
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    sid = first["session_id"]
    old_state, at = talk.store[sid]
    old_state.pop("user_id")  # 배포 전에 저장된 대화 상태
    talk.user_id = other
    res = talk.say("요약해줘")
    assert res["session_id"] != sid and res["summary"] is None
    assert res["answer"] != first["answer"]
    assert talk.store[sid] == (old_state, at) and "user_id" not in old_state
    assert talk.store[res["session_id"]][0]["user_id"] == other
    assert "새 대화로 시작했습니다" in res["notice"]


@pytest.mark.parametrize("other", [8, None])
def test_someone_else_cannot_continue_a_users_session(talk: Talk, other: int | None) -> None:
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    talk.user_id = other  # 다른 계정, 또는 로그아웃한 손님이 같은 대화 id 로 묻는다
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["session_id"] != first["session_id"]  # 새 대화로 답한다
    assert res["request_type"] == "new" and talk.extracted == []  # 앞 조건을 이어받지 않는다
    assert "새 대화로 시작했습니다" in res["notice"]
    # 원래 대화는 그대로 남아 주인이 이어 갈 수 있다
    assert talk.store[first["session_id"]][0]["user_id"] == 7
    talk.user_id, talk.sid = 7, first["session_id"]
    again = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert again["session_id"] == first["session_id"] and again["request_type"] == "change"


def test_guest_session_continues_after_login(talk: Talk) -> None:
    talk.user_id = None
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    assert talk.store[first["session_id"]][0]["user_id"] is None
    talk.user_id = 7  # 도중에 로그인: 하던 대화를 그대로 이어 가고, 그 뒤로는 이 사용자의 대화다
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["session_id"] == first["session_id"] and res["request_type"] == "change"
    assert res["notice"] is None or "새 대화" not in res["notice"]
    assert talk.store[first["session_id"]][0]["user_id"] == 7
    talk.user_id = None
    assert (
        talk.say("남자만", request_type="change", changes={"sex": "남"})["session_id"]
        != first["session_id"]
    )


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


# ---- 수치와 통계표를 한 번에 (table_guide) ----------------------------------------------


def test_numeric_answer_carries_its_table_card(talk: Talk) -> None:
    res = talk.say("부산 출생아 최근 5년 보여 줘")
    assert res["status"] == "ok" and res["chart"]["type"] == "line"
    assert res["tables"] == {"status": "ok", "recommendations": [{"tbl_id": "T", "basis": "기준"}]}
    assert talk.searched == []  # 수치로 답했으면 통계표 찾기(HCX)를 부르지 않는다


def test_unsupported_indicator_falls_back_to_table_search(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    res = talk.say("반려동물 키우는 집 몇 곳이야?")
    assert res["status"] == "tables" and res["chart"] is None and res["applied"] == []
    assert res["tables"]["recommendations"] == [{"tbl_id": "DT_X"}]
    assert "수치를 바로 계산하지 못해" in res["answer"]
    question, search_session, _ = talk.searched[0]
    assert question == "반려동물 키우는 집 몇 곳이야?" and search_session.startswith("S_")
    # 표만 안내한 뒤의 말은 수치 조건 변경이 아니라 새 질문이다
    talk.parsed = dict(BASE)
    assert talk.say("부산 출생아 최근 5년")["request_type"] == "new" and talk.extracted == []


def test_unsupported_indicator_computed_from_found_table(talk: Talk) -> None:
    # 사전에 없는 지표: 통계표 찾기가 고른 표로 바로 계산했으면 표 안내 대신 수치로 답한다
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    spec = {"name": "1인 가구 수", "dynamic": True}
    talk.dynamic = {
        "status": "ok",
        "answer": "2020년 서울특별시의 1인 가구 수는 1,390,701가구입니다.",
        "parsed": {**BASE, "type": "value", "indicator": "1인 가구 수", "dynamic_spec": spec},
        "result": {"status": "ok", "value": 1390701, "method": "dynamic"},
        "sources": [{"name": "1인가구", "basis": "기준", "tbl_id": "DT_1PL1502", "url": None}],
        "trace": {},
    }
    res = talk.say("2020년 서울 1인 가구 수")
    assert res["status"] == "ok" and res["result"]["method"] == "dynamic"
    assert res["request_type"] == "new" and res["search_session"].startswith("S_")
    assert res["tables"] == {
        "status": "ok",
        "recommendations": [{"tbl_id": "DT_1PL1502", "basis": "기준"}],
    }
    # 다음 턴은 바로 만든 정의를 조건으로 이어 간다 (부산도 같이 → 변경)
    saved = talk.store[res["session_id"]][0]["conditions"]
    assert saved["indicator"] == "1인 가구 수" and saved["dynamic_spec"] == spec


def test_cached_plan_answers_without_table_search(talk: Talk) -> None:
    # 전에 바로 계산한 지표: 통계표 찾기(HCX 3회) 없이 저장한 표로 계산한다
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    talk.cached = {
        "status": "ok",
        "answer": "답",
        "parsed": {**BASE, "type": "value", "indicator": "1인 가구 수"},
        "result": {"status": "ok", "value": 1, "method": "dynamic"},
        "sources": [{"name": "1인가구", "basis": "기준", "tbl_id": "DT_1PL1502", "url": None}],
        "trace": {},
    }
    res = talk.say("2020년 서울 1인 가구 수")
    assert res["status"] == "ok" and res["result"]["method"] == "dynamic"
    assert talk.searched == []
    assert res["tables"]["recommendations"] == [{"tbl_id": "DT_1PL1502", "basis": "기준"}]


def test_ungrounded_dictionary_answer_replaced_by_dynamic(talk: Talk) -> None:
    # HCX가 질문에 근거 없는 사전 지표를 고름 (예: "1인 가구 중 여성 비율" → 총인구 성별 구성)
    talk.parsed = {**BASE, "type": "value", "indicator": "출생아 수", "grounded": False}
    talk.dynamic = {
        "status": "ok",
        "answer": "1인 가구 중 여성 비율은 53.64%입니다.",
        "parsed": {**BASE, "type": "value", "indicator": "1인 가구 중 여성 비율"},
        "result": {"status": "ok", "value": 53.64, "method": "dynamic"},
        "sources": [{"name": "1인가구", "basis": "기준", "tbl_id": "DT_1PL1502", "url": None}],
        "trace": {},
    }
    res = talk.say("서울 1인 가구 중 여성 비율")
    assert res["result"]["method"] == "dynamic" and res["request_type"] == "new"
    assert len(talk.searched) == 1


def test_ungrounded_dictionary_out_of_range_not_returned(talk: Talk) -> None:
    # 근거 없는 사전 지표의 '자료 기간 밖' 안내도 질문과 맞지 않는다 → 통계표 안내
    talk.parsed = {**BASE, "type": "value", "indicator": "출생아 수", "grounded": False}
    talk.fail_when = lambda parsed: True  # 가짜 qa: out_of_range
    res = talk.say("2020년 서울 1인 가구 수")
    assert res["status"] == "tables" and res["trace"]["replaced"]["by"] == "tables"


def test_ungrounded_dictionary_answer_not_returned_when_dynamic_fails(talk: Talk) -> None:
    # 질문에 근거 없는 사전 지표 답은 바로 계산이 안 돼도 돌려주지 않고 통계표를 안내한다
    talk.parsed = {**BASE, "type": "value", "indicator": "출생아 수", "grounded": False}
    res = talk.say("서울 1인 가구 중 여성 비율")
    assert res["status"] == "tables" and res["tables"]["recommendations"] == [{"tbl_id": "DT_X"}]
    assert res["trace"]["replaced"]["indicator"] == "출생아 수"
    # 근거가 있는 사전 지표는 통계표 찾기를 부르지 않는다
    talk.parsed = {**BASE, "type": "value", "indicator": "출생아 수", "grounded": True}
    searched = len(talk.searched)
    talk.say("부산 출생아 수")
    assert len(talk.searched) == searched


def test_table_cell_clarification_then_pick(talk: Talk, monkeypatch: pytest.MonkeyPatch) -> None:
    # 찾은 표의 분류에 합계가 없어 칸을 되묻고, 고른 칸으로 같은 표를 다시 계산한다
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    talk.dynamic = dynamic_indicator.ask_answer(
        "서울 1인 가구 수",
        dict(BASE),
        "DT_X",
        "1인가구",
        dynamic_indicator.Ask(3, "거주기간별", [("1", "1년 미만"), ("2", "1~5년")]),
    )
    first = talk.say("서울 1인 가구 수")
    assert first["status"] == "clarify_question"
    assert first["clarification"]["options"] == ["1년 미만", "1~5년"]
    picked = []

    async def fake_choice(question, context, code, trace):
        picked.append((question, context["tbl_id"], code))
        return {
            "status": "ok",
            "answer": "답",
            "parsed": {**BASE, "type": "value", "indicator": "1인 가구 수"},
            "result": {"status": "ok", "value": 1, "method": "dynamic"},
            "sources": [],
            "trace": {},
        }

    monkeypatch.setattr(dynamic_indicator, "answer_choice", fake_choice)
    asked = len(talk.asked)
    selections = {"clarification_kind": "table_cell", "clarification_value": "1~5년"}
    res = talk.say("1~5년", selections=selections)
    assert res["status"] == "ok" and res["request_type"] == "reply"
    assert picked == [("서울 1인 가구 수", "DT_X", "2")]
    assert len(talk.asked) == asked  # 고를 때는 질문 해석(HCX)을 다시 부르지 않는다


@pytest.mark.parametrize("refused", ["out_of_scope", "not_statistics"])
def test_out_of_scope_is_refused_not_table_guide(talk: Talk, refused: str) -> None:
    # 통계표 찾기도 범위 밖으로 끝나면 표 안내(tables)가 아니라 거절로 답한다
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}  # 가짜 qa가 모르는 지표
    talk.found = {"status": refused, "answer": "제공 범위에서 찾지 못했습니다."}
    res = talk.say("서울 아파트 값 알려줘")
    assert res["status"] == refused and res["chart"] is None
    assert "수치를 바로 계산하지 못해" not in res["answer"]
    # 거절 뒤의 말도 수치 조건 변경이 아니라 새 질문이다
    talk.parsed = dict(BASE)
    assert talk.say("부산 출생아 최근 5년")["request_type"] == "new" and talk.extracted == []


def test_table_choice_reply_goes_to_table_search(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    talk.found = {"status": "clarify", "answer": "대상을 골라 주세요."}
    talk.say("국적 취득한 외국인 통계")
    session = talk.searched[0][1]
    talk.found = {"status": "ok", "answer": "찾았습니다."}
    res = talk.say("혼인 귀화자, 여", selections={"target": "혼인 귀화자", "sex": "여"})
    assert res["status"] == "tables" and res["request_type"] == "reply"
    assert talk.searched[-1] == ("혼인 귀화자, 여", session, {"target": "혼인 귀화자", "sex": "여"})
    assert len(talk.asked) == 1  # 선택지에 답할 때는 수치 해석(HCX)을 부르지 않는다


def test_chart_change_does_not_search_again(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("표로 보여줘")
    assert res["request_type"] == "chart" and "tables" not in res and talk.searched == []


def test_new_question_without_indicator_inherits_it() -> None:
    """기간·방식만 말한 새 질문은 앞 지표를 이어받는다.
    질문에 있는 말을 지표로 적었으면 이어받지 않는다"""
    prev = {**BASE, "type": "compare", "indicator": "고령인구 비율", "regions": ["서울", "부산"]}
    prev_sources = {**USER}
    parsed = {
        "type": "trend",
        "indicator": None,
        "term": "추이",
        "regions": [],
        "start_year": 2015,
        "end_year": 2024,
        "year": 2024,
    }
    question = "2015년부터 추이로 보여줘"
    p, src = s.inherit(parsed, s.sources_of(question, parsed), prev, prev_sources, question)
    assert p["indicator"] == "고령인구 비율" and src["indicator"] == "inherited"
    assert p["regions"] == ["서울", "부산"] and p["start_year"] == 2015
    pets = {**parsed, "term": "반려동물", "indicator": "반려동물 가구"}
    q2 = "반려동물 키우는 집 추이"
    p2, src2 = s.inherit(pets, s.sources_of(q2, pets), prev, prev_sources, q2)
    assert p2["indicator"] == "반려동물 가구" and src2["indicator"] == "user"
    # 지표를 말하지 않았는데 HCX가 총인구를 채워 넣어도 앞 지표를 이어받는다
    guessed = {**parsed, "indicator": "총인구", "term": None}
    p3, _ = s.inherit(guessed, s.sources_of(question, guessed), prev, prev_sources, question)
    assert p3["indicator"] == "고령인구 비율"


@pytest.mark.parametrize(
    ("question", "year"),
    [
        ("그 전년도는?", 2019),  # 지금 보는 연도(2020) 기준
        ("전년도는?", 2019),
        ("그 다음 해는?", 2021),
        ("이듬해는?", 2021),
        ("작년은?", 2025),  # 오늘(2026-11-12) 기준
        ("올해는?", 2026),
        ("재작년은?", 2024),
        ("3년 전은?", 2023),
        ("전년 대비 얼마나 늘었어?", None),  # 비교 기준이지 시점이 아니다
        ("2018년은?", None),  # 숫자로 말한 연도는 HCX가 그대로 적는다
        ("작년부터 보여줘", None),  # 기간
        ("부산은?", None),
    ],
)
def test_follow_year(question, year) -> None:
    from datetime import date

    value = {**BASE, "type": "value", "year": 2020, "start_year": None, "end_year": None}
    assert s.follow_year(question, value, today=date(2026, 11, 12)) == year


def test_relative_year_overrides_hcx(talk: Talk, monkeypatch: pytest.MonkeyPatch) -> None:
    """HCX가 "작년은?"을 앞 연도의 다음 해로 읽어도 코드가 오늘 기준으로 고친다"""
    monkeypatch.setattr(s, "follow_year", lambda q, c, today=None: 2025 if "작년" in q else None)
    talk.say("2020년 부산 출생아 수")
    res = talk.say("작년은?", request_type="change", changes={"year": 2021})
    assert res["parsed"]["year"] == 2025 and res["request_type"] == "change"


def test_inherited_region_falls_back_to_original_scope(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "regions": ["수성구"]}
    talk.say("2024년 수성구 출생아 수")
    talk.parsed = {**BASE, "type": "value", "regions": [], "year": 2050}
    talk.fail_when = lambda p: p.get("regions") == ["수성구"]
    res = talk.say("2050년 출생아 수")
    assert res["status"] == "ok" and res["parsed"]["regions"] == []
    assert "지역(수성구)" in res["notice"] and "전국" in res["notice"]
    assert len(talk.answered) == 1


def test_explicit_region_is_not_dropped(talk: Talk) -> None:
    talk.say("부산 출생아 2020~2024년")
    talk.parsed = {**BASE, "type": "value", "regions": ["수성구"], "year": 2050}
    talk.fail_when = lambda p: True
    res = talk.say("2050년 수성구 출생아 수")
    assert res["status"] == "out_of_range" and talk.answered == []


def test_table_pending_does_not_swallow_new_numeric_question(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    talk.found = {"status": "clarify", "answer": "대상을 골라 주세요."}
    talk.say("외국인 통계")
    talk.parsed = {**BASE, "type": "value", "indicator": "합계출산율", "regions": ["서울"]}
    res = talk.say("서울 합계출산율은?")
    assert res["status"] == "ok" and res["request_type"] == "new"
    assert len(talk.searched) == 1


def test_choice_reply_requires_actual_option() -> None:
    options = ["혼인 귀화자", "여"]
    assert s.is_choice_reply("혼인 귀화자요", options)
    assert s.is_choice_reply("혼인 귀화자, 여", options)
    assert not s.is_choice_reply("서울 합계출산율은?", options)
    assert not s.is_choice_reply("새 질문", options)


def test_both_inherited_conditions_can_be_dropped_without_mutating_input() -> None:
    sources = {**USER, "period": "inherited", "regions": "inherited"}
    original = {**BASE, "regions": [], "start_year": 2021}
    tries = list(s.without_inherited(BASE, original, sources))
    assert [t[2] for t in tries] == [("period",), ("regions",), ("period", "regions")]
    assert tries[-1][0]["regions"] == [] and tries[-1][0]["start_year"] == 2021
    assert BASE["regions"] == ["부산"] and sources["regions"] == "inherited"


def test_table_pending_keeps_options_for_text_reply(talk: Talk, monkeypatch) -> None:
    async def find(question, session_id, selections, user_id):
        talk.searched.append((question, session_id, selections))
        return {
            "status": "clarify",
            "answer": "대상을 골라 주세요.",
            "clarify": {"axes": [{"options": ["혼인 귀화자", "여"]}]},
        }

    monkeypatch.setattr(table_guide, "find_tables", find)
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    talk.say("외국인 통계")
    assert talk.store[talk.sid][0]["pending"]["options"] == ["혼인 귀화자", "여"]
    res = talk.say("혼인 귀화자요")
    assert res["request_type"] == "reply" and len(talk.asked) == 1


def test_question_interpretation_records_usage(monkeypatch) -> None:
    from types import SimpleNamespace

    sent = {}

    async def chat(self, *args, **kwargs):
        sent.update(kwargs)
        return SimpleNamespace(content="{}", usage={"completionTokens": 123})

    monkeypatch.setattr(qa.HcxClient, "chat", chat)
    trace = {}
    assert asyncio.run(qa._chat("질문", "시스템", trace=trace)) == "{}"
    assert trace["understand_usage"] == {"completionTokens": 123}
    # 짧은 JSON만 받으므로 추론을 끄고 응답 길이를 줄인다 (HCX 한도를 미리 덜 잡게)
    assert trace["understand_max_completion_tokens"] == 1024
    assert sent["maxCompletionTokens"] == 1024
    assert sent["thinking"] == {"effort": "none"}


@pytest.mark.parametrize("region", ["서울", "전국"])
def test_complete_question_skips_change_extraction(talk: Talk, region: str) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    question = f"2024년 {region} 합계출산율을 막대그래프로 보여줘"
    res = talk.say(question)
    assert res["request_type"] == "new"
    assert talk.asked[-1] == question
    assert talk.extracted == []


@pytest.mark.parametrize(
    "question",
    [
        "2024년 서울 합계출산율도 같이 보여줘",
        "2024년 서울 합계출산율 대신 사망자 수 보여줘",
        "서울 합계출산율은?",
        "2024년 합계출산율은?",
        "2024년 서울은?",
    ],
)
def test_context_dependent_question_still_extracts_changes(talk: Talk, question: str) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    talk.say(question)
    assert talk.extracted == [question]


# ---- 의미가 모호한 질문의 확인 --------------------------------------------------------------


def test_local_region_is_asked_before_ai_and_not_inherited(talk: Talk):
    talk.say("2024년 부산 출생아 수")
    calls = list(talk.asked)
    res = talk.say("2024년 우리 동네 출생아 수")
    assert res["status"] == "clarify_question"
    assert res["clarification"]["kind"] == "local_region"
    assert talk.asked == calls and talk.extracted == []
    talk.parsed = {**BASE, "regions": ["서울"]}
    res = talk.say("서울이요")
    assert res["request_type"] == "reply"
    assert talk.asked[-1] == "2024년 서울 출생아 수"
    assert "우리 동네" not in res["question"]
    assert talk.store[talk.sid][0]["pending"] is None


@pytest.mark.parametrize("reply,indicator", [("포함해줘", "이동자 수"), ("2번", "전출자 수")])
def test_movement_scope_answers_with_matching_indicator_not_net_migration(
    talk: Talk, reply, indicator
):
    talk.parsed = {**BASE, "indicator": "순이동", "regions": ["부산"]}  # HCX가 순이동으로 읽어도
    res = talk.say("2024년 부산에서 이사한 건수는?")
    assert res["clarification"]["kind"] == "move_scope"
    assert talk.asked == talk.extracted == []
    res = talk.say(reply)
    assert res["status"] == "ok" and res["parsed"]["indicator"] == indicator
    assert talk.asked == ["2024년 부산에서 이사한 건수는?"] and talk.searched == []


def test_movement_falls_back_to_tables_when_not_computable(talk: Talk):
    talk.parsed = {**BASE, "regions": ["부산"]}
    talk.fail_when = lambda parsed: parsed.get("indicator") == "전출자 수"
    talk.say("2024년 부산에서 이사한 건수는?")
    res = talk.say("2번")
    assert res["status"] == "tables" and "나간 전출 이동자수" in talk.searched[-1][0]
    assert "통계표" in res["notice"]


def test_movement_scope_button_and_local_then_scope(talk: Talk):
    res = talk.say("2024년 우리 동네에서 이사한 건수")
    assert res["clarification"]["kind"] == "local_region"
    res = talk.say("부산")
    assert res["clarification"]["kind"] == "move_scope"
    res = talk.say(
        "선택",
        selections={
            "clarification_kind": "move_scope",
            "clarification_value": "지역 안에서의 이사도 포함",
        },
    )
    assert res["status"] == "ok" and res["parsed"]["indicator"] == "이동자 수"
    assert talk.asked[-1] == "2024년 부산에서 이사한 건수"


def test_uncertain_clarification_reply_keeps_question_but_new_question_cancels(talk: Talk):
    talk.say("우리 동네 출생아 수")
    assert talk.say("모르겠어")["status"] == "clarify_question"
    assert talk.asked == []
    talk.parsed = {**BASE, "regions": ["서울"]}
    res = talk.say("2024년 서울 합계출산율 알려줘")
    assert res["status"] == "ok" and talk.asked == ["2024년 서울 합계출산율 알려줘"]
    assert talk.store[talk.sid][0]["pending"] is None


def test_clarification_is_asked_at_most_twice_then_defaults(talk: Talk):
    # 이사 범위: 두 번까지 묻고, 그래도 모르면 '밖으로 나간 이사만'으로 찾고 알린다
    talk.say("부산에서 이사한 건수")
    assert talk.say("응")["status"] == "clarify_question"
    res = talk.say("모르겠어")
    assert res["status"] == "ok" and res["parsed"]["indicator"] == "전출자 수"
    assert "알아듣지 못해" in res["notice"]
    assert talk.store[talk.sid][0]["pending"] is None
    # 지역: 두 번까지 묻고, 그래도 모르면 답하지 않고 다시 질문하라고 알린다
    talk.say("우리 동네 출생아 수")
    assert talk.say("모르겠어")["status"] == "clarify_question"
    res = talk.say("글쎄")
    assert res["status"] == "unknown_region" and "다시 질문" in res["answer"]
    assert talk.asked == ["부산에서 이사한 건수"] and talk.store[talk.sid][0]["pending"] is None


def test_stated_scope_answers_without_asking_not_net_migration(talk: Talk):
    talk.parsed = {**BASE, "indicator": "순이동", "regions": ["수원"]}
    res = talk.say("수원에서 이사 나간 사람 몇 명이야?")
    assert res["status"] == "ok" and res["parsed"]["indicator"] == "전출자 수"
    res = talk.say("전국에서 이사한 사람 수")
    assert res["status"] == "ok" and res["parsed"]["indicator"] == "이동자 수"
    assert talk.searched == []


def test_region_reply_without_suffix_and_ambiguous_name(talk: Talk):
    talk.say("2024년 우리 동네 출생아 수")
    talk.parsed = {**BASE, "regions": ["경기도 수원시"]}
    res = talk.say("수원")
    assert res["request_type"] == "reply" and talk.asked[-1] == "2024년 경기도 수원시 출생아 수"
    # 같은 이름이 여러 곳이면 후보를 선택지로 다시 묻는다
    talk.say("2024년 우리 동네 출생아 수")
    res = talk.say("고성")
    assert res["status"] == "clarify_question"
    assert {"강원특별자치도 고성군", "경상남도 고성군"} <= set(res["clarification"]["options"])


def test_scope_pending_does_not_swallow_unsupported_new_question(talk: Talk):
    talk.say("부산에서 이사한 건수")
    talk.parsed = {**BASE, "indicator": "반려동물 수"}
    res = talk.say("강아지 통계 알려줘")
    assert res["status"] == "tables"
    assert talk.asked == ["강아지 통계 알려줘"]
    assert "집계 범위" not in talk.searched[-1][0]


def test_region_clarification_keeps_compatible_previous_period(talk: Talk):
    talk.say("부산 출생아 수 2020년부터 2024년까지")
    talk.say("우리 동네 출생아 수 추이는?")
    talk.parsed = {**BASE, "regions": ["서울"], "start_year": 2022, "end_year": 2025}
    res = talk.say("서울")
    assert res["parsed"]["regions"] == ["서울"]
    assert (res["parsed"]["start_year"], res["parsed"]["end_year"]) == (2020, 2024)
    assert "기간" in res["notice"]


def test_invalid_region_chart_has_notice_and_keeps_numeric_answer(talk: Talk, monkeypatch):
    async def answer(question, adjust=None):
        return {
            "status": "ok",
            "answer": "서울·부산의 계산 결과입니다.",
            "parsed": dict(BASE),
            "sources": [],
            "trace": {},
            "result": {
                "status": "ok",
                "split_by": "region",
                "split": [
                    {
                        "status": "ok",
                        "indicator": "인구",
                        "unit": "명",
                        "region": "서울",
                        "series": [{"year": 2023, "value": 1}, {"year": 2024, "value": 2}],
                    },
                    {
                        "status": "ok",
                        "indicator": "인구",
                        "unit": "%",
                        "region": "부산",
                        "series": [{"year": 2023, "value": 1}, {"year": 2024, "value": 2}],
                    },
                ],
            },
        }

    monkeypatch.setattr(qa, "ask", answer)
    res = talk.say("지역별 인구 추이")
    assert res["chart"] is None
    assert res["answer"] == "서울·부산의 계산 결과입니다."
    assert "기준이 달라" in res["notice"]
    assert len(res["result"]["split"]) == 2


def test_detail_request_repeats_the_whole_answer_not_the_summary(talk: Talk) -> None:
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    for said in ("더 자세히 설명해줘", "요약하지 말고 더 자세히 알려줘"):
        res = talk.say(said)
        assert res["request_type"] == "detail" and res["answer"] == first["answer"]
        assert res["chart"] == first["chart"] and res["sources"] == first["sources"]
    assert talk.extracted == [] and talk.answered == []  # HCX도 계산도 부르지 않는다
    assert not s.detail_request("서울 출생아 수 자세히 알려줘")  # 지표를 말하면 질문이다
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["parsed"]["regions"] == ["부산"]  # 조건은 그대로 이어진다


def test_summary_with_a_change_answers_the_changed_conditions(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("2022년만 요약해줘", request_type="summary", changes={"year": 2022})
    assert res["request_type"] == "change" and res["parsed"]["year"] == 2022
    assert len(talk.answered) == 1  # 앞 답의 요약이 아니라 바뀐 조건으로 다시 계산한다


def test_summary_with_nothing_to_summarize_is_not_read_as_a_question(talk: Talk) -> None:
    res = talk.say("요약해줘")  # 첫 말
    assert res["status"] == "unclear" and res["request_type"] == "unclear"
    assert res["answer"].startswith("요약할 답이 아직 없습니다.") and res["summary"] is None
    assert talk.asked == [] and talk.extracted == []  # HCX를 부르지 않는다
    res = talk.say("부산 출생아 최근 5년 보여 줘")  # 그 뒤 질문은 그대로 새 질문
    assert res["status"] == "ok" and res["request_type"] == "new"


def test_summary_after_tables_only_answer_repeats_its_lead(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    first = talk.say("반려동물 키우는 집 몇 곳이야?")
    assert first["status"] == "tables"
    asked = len(talk.asked)
    res = talk.say("요약해줘")
    assert res["request_type"] == "summary" and res["summary"] is None
    assert res["answer"] == first["answer"].split("\n\n")[0]
    assert len(talk.asked) == asked and len(talk.searched) == 1  # 다시 해석·검색하지 않는다


# ---- 돌려 말한 말: 인사, 출처, 원인, 지표만 바꾸기, 성별 구분 없는 지표 --------------------


def test_bare_indicator_question_keeps_the_view_and_period() -> None:
    """ "사망자 수는?"처럼 지표만 말하면 보는 방식(추이)·기간·지역을 그대로 이어받는다.
    전에는 값 하나로 읽어 두 지역 추이 뒤에 첫 지역의 한 해 값만 답했다"""
    prev = {**BASE, "regions": ["부산", "서울"]}
    new = {"type": "value", "indicator": "사망자 수", "regions": [], "year": 2025, "sex": "계"}
    plain = {"indicator": "user", "regions": "default", "period": "default", "sex": "default"}
    p, src = s.inherit(new, plain, prev, USER, "사망자 수는?")
    assert p["type"] == "trend" and p["regions"] == ["부산", "서울"]
    assert (p["start_year"], p["end_year"]) == (2020, 2024) and p["indicator"] == "사망자 수"
    assert src["regions"] == "inherited" and src["period"] == "inherited"
    # 보는 방식을 말했으면 그 방식대로 (기간은 방식이 다르면 이어받지 않는다)
    p, src = s.inherit(new, plain, prev, USER, "사망자 수가 가장 많은 곳은?")
    assert p["type"] == "compare" and p["year"] == 2025 and src["period"] == "default"


def test_value_question_with_several_inherited_regions_compares_them() -> None:
    prev = {**BASE, "regions": ["부산", "서울"]}
    new = {"type": "value", "indicator": "사망자 수", "regions": [], "year": 2022, "sex": "계"}
    said = {"indicator": "user", "regions": "default", "period": "user", "sex": "default"}
    p, src = s.inherit(new, said, prev, USER, "2022년 사망자 수는?")
    assert p["type"] == "compare" and p["regions"] == ["부산", "서울"] and p["year"] == 2022
    one = s.inherit(new, said, BASE, USER, "2022년 사망자 수는?")[0]
    assert one["type"] == "value" and one["regions"] == ["부산"]  # 한 지역이면 값 하나 그대로


def test_greeting_is_answered_without_hcx_and_keeps_conditions(talk: Talk) -> None:
    res = talk.say("안녕하세요")  # 첫 말
    assert res["status"] == "chat" and res["request_type"] == "chat" and res["sources"] == []
    talk.say("부산 출생아 최근 5년 보여 줘")
    for said in ("고마워", "감사합니다!", "네", "ㅇㅋ"):
        res = talk.say(said)
        assert res["request_type"] == "chat" and "이어서 질문해 주세요" in res["answer"], said
        assert [a["value"] for a in res["applied"]][:2] == ["출생아 수", "부산"]
    assert len(talk.asked) == 1 and talk.extracted == [] and talk.searched == []
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["parsed"]["regions"] == ["부산"]  # 조건은 그대로 이어진다
    assert s.chat_reply("고마워 그런데 서울은?") is None and s.chat_reply("네이버 통계") is None


def test_source_question_repeats_the_source_lines(talk: Talk, monkeypatch) -> None:
    answer = "핵심 문장.\n\n- 2020년: 1\n※ 계산식: A ÷ B\n출처: 통계청 (기준)\n통계표: https://kosis.kr/x"
    result = talk._result
    monkeypatch.setattr(talk, "_result", lambda *a: {**result(*a), "answer": answer})
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    for said in ("이 통계 출처가 뭐야?", "언제 기준이야?", "어떻게 계산한 거야?"):
        res = talk.say(said)
        assert res["request_type"] == "source" and res["sources"] == first["sources"], said
        assert res["answer"].split("\n")[1:] == [
            "※ 계산식: A ÷ B",
            "출처: 통계청 (기준)",
            "통계표: https://kosis.kr/x",
        ]
    assert talk.extracted == [] and talk.answered == [] and talk.searched == []
    assert not s.source_request("출생아 수 출처 통계표 찾아줘")  # 지표를 말하면 질문이다


def test_why_question_says_no_cause_and_repeats_the_summary(talk: Talk) -> None:
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("왜 늘어난 거야?")
    assert res["request_type"] == "summary" and talk.extracted == []
    assert res["answer"].split("\n")[:2] == [s.NO_CAUSE, first["summary"]["headline"]]
    assert not s.why_request("왜 서울 출생아 수가 줄었어?")  # 지표를 말하면 새 질문 (qa의 causal)


def test_sex_that_the_data_does_not_split_is_told() -> None:
    result = {"status": "ok", "sex": "계", "value": 1}
    assert s.sex_not_split({"sex": "남"}, result)
    assert s.effective({"sex": "남", "type": "value"}, result)["sex"] == "계"
    assert not s.sex_not_split({"sex": "남"}, {**result, "sex": "남"})
    assert not s.sex_not_split({"sex": "계"}, result)
    assert not s.sex_not_split({"sex": "남"}, {"status": "out_of_range"})
    split = {
        "status": "ok",
        "split": [{"status": "ok", "sex": "계"}, {"status": "ok", "sex": "남"}],
    }
    assert not s.sex_not_split({"sex": "남"}, split)


def test_composition_view_request_changes_the_view_without_hcx(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("구성비로 보여줘")
    assert res["request_type"] == "change" and res["parsed"]["type"] == "composition"
    assert talk.extracted == [] and res["parsed"]["regions"] == ["부산"]
    assert not s.composition_request("2022년 구성비로 보여줘")  # 연도·지표를 말하면 HCX에 맡긴다
    assert not s.composition_request("출생아 수 구성비 알려줘")


# ---- PR #136 리뷰: 자세히·출처·원인 규칙이 다른 뜻의 말까지 잡던 경우 ----------------------

NOT_ABOUT_LAST = [
    # 새 통계표를 찾는 말 (출처 질문이 아니다)
    "다문화 관련 어떤 통계가 있어?",
    "이민자는 어떤 자료가 있어?",
    "어느 통계표를 보면 돼?",
    "외국인은 무슨 조사로 알 수 있어?",
    # 지역 이름·새 질문 (원인 질문이 아니다)
    "왜관읍은?",
    "칠곡군 왜관읍도 같이",
    "고령화 영향 큰 곳은?",
    "정책 효과 있는 지역은?",
    # 나눠서 보자는 말 (직전 답을 다시 달라는 말이 아니다)
    "구별로 자세히 보여줘",
    "연령별로 자세하게",
    "남녀로 나눠서 구체적으로",
    # 조건을 함께 말함
    "서울은 왜 줄었어?",
    "2022년은 출처가 뭐야?",
    "부산만 자세히",
]


@pytest.mark.parametrize("said", NOT_ABOUT_LAST)
def test_rules_leave_other_meanings_to_hcx(talk: Talk, said: str) -> None:
    """규칙은 HCX 없이 바로 답하고 끝나므로, 직전 답만 가리키는 말이 아니면 잡지 않는다"""
    assert not (s.detail_request(said) or s.source_request(said) or s.why_request(said)), said
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say(said)
    assert res["request_type"] not in ("detail", "source", "summary"), said
    if said == "구별로 자세히 보여줘":  # 보던 시도 안의 시군구로 조건 변경
        assert res["request_type"] == "change" and res["parsed"]["sido"] == "부산"
        assert res["parsed"]["level"] == "시군구" and len(talk.answered) == 1
    elif res["status"] == "tables":  # 통계표를 찾는 말은 통계표 찾기로
        assert [q for q, *_ in talk.searched] == [said]
    else:
        assert talk.extracted == [said]  # 나머지는 HCX 변경 추출로 넘어갔다


@pytest.mark.parametrize(
    ("said", "rule"),
    [
        ("더 자세히 설명해줘", "detail"),
        ("요약하지 말고 더 자세히 알려줘", "detail"),
        ("이 통계 출처가 뭐야?", "source"),
        ("어디서 나온 숫자야?", "source"),
        ("언제 기준이야?", "source"),
        ("무슨 자료야?", "source"),
        ("이 답은 어느 통계표에서 나온 거야?", "source"),
        ("왜 줄어든 거야?", "why"),
        ("왜?", "why"),
        ("이유가 뭐야?", "why"),
    ],
)
def test_rules_still_catch_remarks_about_the_last_answer(said: str, rule: str) -> None:
    hit = {"detail": s.detail_request, "source": s.source_request, "why": s.why_request}
    assert [name for name, test in hit.items() if test(said)] == [rule], said


@pytest.mark.parametrize(
    "said",
    [
        "다문화 관련 어떤 통계가 있어?",
        "이민자는 어떤 자료가 있어?",
        "어느 통계표를 보면 돼?",
        "외국인은 무슨 조사로 알 수 있어?",
    ],
)
def test_table_seeking_question_goes_to_table_search(talk: Talk, said: str) -> None:
    """수치 답 뒤에 다른 주제의 통계표를 물으면 앞 지표·지역을 이어받지 않고 통계표를 찾는다"""
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say(said)
    assert res["status"] == "tables" and res["request_type"] == "new" and res["applied"] == []
    assert res["tables"]["recommendations"] == [{"tbl_id": "DT_X"}]
    assert [q for q, *_ in talk.searched] == [said] and len(talk.asked) == 1
    assert not s.table_question("이 답은 어느 통계표에서 나온 거야?")  # 출처 질문
    assert not s.table_question("출생아 수 통계표 찾아줘")  # 지표를 말하면 수치 질문 경로


def test_gu_ranking_stays_in_the_province_being_viewed() -> None:
    """ "구별로 보여줘": HCX가 질문에 없는 시도(서울)를 채워도 앞에서 보던 시도 안에서 본다"""
    rank = {**BASE, "type": "rank", "regions": [], "level": "시군구", "sido": "서울"}
    plain = {"indicator": "user", "regions": "default", "period": "default", "sex": "default"}
    p, src = s.inherit(rank, plain, BASE, USER, "구별로 보여줘")
    assert p["sido"] == "부산" and src["regions"] == "inherited"
    said = s.inherit(rank, plain, BASE, USER, "서울 구별로 보여줘")[0]
    assert said["sido"] == "서울"  # 말한 시도는 그대로
    two = {**BASE, "regions": ["부산", "서울"]}
    assert s.inherit(rank, plain, two, USER, "구별로 보여줘")[0]["sido"] is None  # 보던 곳이 여럿


def test_by_district_request_ranks_inside_the_province_being_viewed(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("구별로 자세히 보여줘")
    assert res["request_type"] == "change" and talk.extracted == [] and len(talk.asked) == 1
    p = res["parsed"]
    assert (p["type"], p["level"], p["sido"], p["regions"]) == ("rank", "시군구", "부산", [])
    assert p["indicator"] == "출생아 수"
    res = talk.say("시도별로 보여줘")
    assert (res["parsed"]["level"], res["parsed"]["sido"]) == ("시도", None)
    # 보던 지역이 여럿이거나 지역·지표·연도를 함께 말하면 HCX에 맡긴다
    assert s.region_split_changes("구별로 보여줘", {**BASE, "regions": ["부산", "서울"]}) is None
    assert s.region_split_changes("서울 구별로 보여줘", BASE) is None
    assert s.region_split_changes("구별 출생아 수", BASE) is None


@pytest.mark.parametrize(
    "said",
    [
        "남녀로 나눠서 구별로 자세히 보여줘",
        "연령별로 구별로 보여줘",
        "구별로 낮은 순으로 보여줘",
        "구별로 상위 3개만 보여줘",
        "구별로 비교해서 보여줘",
        "인구별로 자세히 보여줘",
        "2022년 구별로 보여줘",
    ],
)
def test_region_split_keeps_additional_conditions_for_hcx(talk: Talk, said: str) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    assert s.region_split_changes(said, BASE) is None
    res = talk.say(said)
    assert res["request_type"] not in ("detail", "source", "summary")
    assert talk.extracted == [said]


def test_past_year_out_of_range_tries_other_tables(talk: Talk) -> None:
    # 사전의 표보다 옛 연도 (1980년 이혼 건수, 사전 표는 1990년부터): 그 연도가 있는 표로 바로 계산
    talk.parsed = {**BASE, "type": "value", "indicator": "이혼 건수"}
    talk.fail_when = lambda parsed: True  # 가짜 qa: out_of_range

    def old(result):
        result["result"].update(available_years=[1990, 2025], asked_year=1980)
        return result

    original = talk._result
    talk._result = lambda parsed, question="": old(original(parsed, question))
    talk.dynamic = {
        "status": "ok",
        "answer": "1980년 서울특별시의 이혼 수는 7,935건입니다.",
        "parsed": {**BASE, "type": "value", "indicator": "이혼"},
        "result": {"status": "ok", "value": 7935, "method": "dynamic"},
        "sources": [{"name": "옛 인구동태", "basis": "기준", "tbl_id": "DT_OLD", "url": None}],
        "trace": {},
    }
    res = talk.say("1980년 서울 이혼 건수")
    assert res["status"] == "ok" and res["result"]["method"] == "dynamic"
    # 바로 계산이 안 되면 '기간 밖' 안내를 그대로 준다 (표 안내로 바꾸지 않음)
    talk.dynamic = None
    res = talk.say("1975년 부산 혼인 건수")
    assert res["status"] == "out_of_range"


@pytest.mark.parametrize(
    ("reply", "selections", "value"),
    [
        ("2번", None, 2023),
        (
            "2024년 기준",
            {"clarification_kind": "alternative", "clarification_value": "2024년 기준"},
            2024,
        ),
    ],
)
def test_alternative_offer_then_pick_returns_the_precomputed_answer(
    talk: Talk, reply: str, selections: dict | None, value: int
) -> None:
    # 조건을 모두 만족하는 값이 없어 대안을 값과 함께 물었다. 고르면 미리 계산한 답을 그대로 준다
    from app.services import alternative

    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}

    def ready(year):
        return {
            "status": "ok",
            "answer": f"{year}년 답",
            "parsed": {**BASE, "type": "value", "indicator": "1인 가구 수", "year": year},
            "result": {"status": "ok", "value": year, "method": "dynamic"},
            "sources": [],
            "trace": {},
        }

    chosen = [
        {
            "label": "2024년 기준",
            "value": "2024년 답",
            "entry": {"tbl_nm": "T"},
            "res": ready(2024),
        },
        {"label": "다른 표", "value": "2023년 답", "entry": {"tbl_nm": "U"}, "res": ready(2023)},
    ]
    talk.dynamic = alternative.answer("2026년 서울 1인 가구 수", dict(BASE), chosen)
    first = talk.say("2026년 서울 1인 가구 수")
    assert first["status"] == "clarify_question"
    assert first["clarification"]["options"] == ["2024년 기준", "다른 표"]
    asked = len(talk.asked)
    res = talk.say(reply, selections=selections)  # 번호로 답하거나 버튼으로 고른다
    assert res["status"] == "ok" and res["request_type"] == "reply"
    assert res["result"]["value"] == value and res["answer"].startswith(f"{value}년 답")
    assert len(talk.asked) == asked  # 고를 때는 질문 해석(HCX)을 다시 부르지 않는다


# ---- 사전이 자료가 없어 답하지 못하면 다른 통계표에서 (table_guide.SEARCH_AFTER, 11/18) ----------


def _computed(value: int) -> dict:
    return {
        "status": "ok",
        "answer": f"다른 표의 값 {value}",
        "parsed": {**BASE, "type": "value", "indicator": "인구 수"},
        "result": {"status": "ok", "value": value, "method": "dynamic"},
        "sources": [{"name": "출처", "basis": "기준", "tbl_id": "DT_Y", "url": None}],
        "trace": {},
    }


def test_no_data_in_dictionary_searches_other_tables(talk: Talk) -> None:
    talk.fail_when = lambda parsed: True  # 가짜 qa: 사전 표 수록 기간 밖 (out_of_range)
    talk.dynamic = _computed(42)
    res = talk.say("부산 출생아 2030년")
    assert res["status"] == "ok" and res["result"]["value"] == 42
    assert res["request_type"] == "new" and talk.searched[-1][0] == "부산 출생아 2030년"


def test_dictionary_notice_is_kept_when_other_tables_do_not_compute(talk: Talk) -> None:
    talk.fail_when = lambda parsed: True
    talk.dynamic = None  # 다른 표로도 계산하지 못함
    res = talk.say("부산 출생아 2030년")
    assert res["status"] == "out_of_range"  # 사전의 안내 그대로
    assert res["tables"]["recommendations"] == [{"tbl_id": "DT_X"}]  # 찾은 표는 카드로


def test_projection_suggestion_and_follow_ups_are_not_searched(talk: Talk, monkeypatch) -> None:
    talk.fail_when = lambda parsed: True
    talk.dynamic = _computed(42)
    res = {
        "status": "out_of_range",
        "request_type": "new",
        "result": {"suggestion": {"status": "ok"}},
    }
    assert not s.searchable_after(res)  # 추계값 제안이 있으면 그대로 (장래추계)
    assert s.searchable_after({**res, "result": {}})
    assert not s.searchable_after({**res, "result": {}, "request_type": "change"})
    for status in ("missing_region", "unknown_region", "data_loading", "error", "ambiguous_region"):
        assert not s.searchable_after({**res, "result": {}, "status": status}), status


def test_dynamic_picking_dictionary_table_keeps_dictionary_answer(monkeypatch) -> None:
    # 바로 계산이 사전 지표의 표(출생아 수 → DT_1B8000H)를 골랐으면 사전 답을 쓴다
    from app.services import ask_session as s

    monkeypatch.setattr(indicators, "spec", lambda n: {"sources": [{"table": "DT_1B8000H"}]})

    parsed = {"indicator": "출생아 수"}
    computed = {"sources": [{"tbl_id": "DT_1B8000H"}]}
    assert s._same_dictionary_table(parsed, computed, None) == "DT_1B8000H"
    asked = {"clarification": {"context": {"tbl_id": "DT_1B8000H"}}}
    assert s._same_dictionary_table(parsed, asked, None) == "DT_1B8000H"
    found = {"recommendations": [{"tbl_id": "DT_1B8000H"}]}
    assert s._same_dictionary_table(parsed, None, found) == "DT_1B8000H"
    other = {"sources": [{"tbl_id": "DT_1B83A35"}]}
    assert s._same_dictionary_table(parsed, other, None) is None


def test_ungrounded_missing_region_goes_to_table_search() -> None:
    # 근거 없는 지표로 "어느 지역인지"를 되묻기보다 통계표 찾기로 넘긴다
    res = {"status": "missing_region", "request_type": s.NEW, "parsed": {"grounded": False}}
    assert s.ungrounded(res) is True
    res["parsed"]["grounded"] = True
    assert s.ungrounded(res) is False


def test_reply_turn_can_search_other_tables() -> None:
    # 되묻기에 답한 턴도 다른 통계표에서 찾는다 (사전은 시도까지만인데 시군구를 받은 경우)
    res = {"status": "missing_data", "request_type": s.REPLY, "result": {}}
    assert s.searchable_after(res) is True
    assert s.searchable_after({**res, "request_type": s.CHART}) is False


def test_alternative_picked_by_its_words() -> None:
    # 대안 되묻기에 번호 대신 대안의 내용으로 답하면 그 대안 (새 질문으로 보내 지역을 잃지 않는다)
    answers = [{"answer": "2024년 부산의 값은 10명입니다.", "result": {"notes": ["귀화자 포함"]}}]
    assert s.alternative_by_words("귀화자까지 포함으로요", answers) == "0"
    assert s.alternative_by_words("응 그걸로", answers) == "0"
    assert s.alternative_by_words("서울은?", answers) is None
    two = [*answers, {"answer": "다른 값", "result": {}}]
    assert s.alternative_by_words("응", two) is None


def test_after_table_choice_computes_with_original_question(monkeypatch) -> None:
    # 표 선택지에 답하면 고른 표로 원래 질문을 바로 계산해 본다 (못 하면 None → 표 안내)
    seen = []

    async def fake_compute(question, parsed, found, trace):
        seen.append(question)
        return {"status": "ok", "answer": "값", "result": {"status": "ok", "value": 1}}

    async def no_cards(res, asked):
        return None

    monkeypatch.setattr(dynamic_indicator, "compute_from_handoff", fake_compute)
    monkeypatch.setattr(s, "_attach_cards", no_cards)
    monkeypatch.setattr(table_guide, "waiting_choice", lambda found: False)
    pending = {"missing": "table", "original_question": "원래 질문", "parsed": {"type": "value"}}
    res = asyncio.run(s._after_table_choice(pending, {"status": "ok"}))
    assert res["question"] == "원래 질문" and res["request_type"] == s.REPLY
    assert seen == ["원래 질문"]
    assert asyncio.run(s._after_table_choice({"missing": "table"}, {"status": "ok"})) is None
