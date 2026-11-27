import asyncio
import copy
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main
from app.core import stats_db
from app.services import ask_session as s
from app.services import dynamic_indicator, indicators, lookup, qa, table_guide
from tests.conftest import memory_turn_writer

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
def follow_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """이어 묻기 설정을 기본값으로 고정한다: .env 에 FOLLOW_RESTATE=true · FOLLOW_LLM=gemma 가
    있어도 테스트는 기존 경로 · HCX 로 시작한다. 켜야 하는 테스트는 restate_on · sure_on 처럼
    그 테스트 안에서 다시 켠다 (autouse 가 먼저 돌고 그 뒤에 켜진다)"""
    settings = s.get_settings()
    monkeypatch.setattr(settings, "follow_restate", False)
    monkeypatch.setattr(settings, "follow_sure_rules", False)
    monkeypatch.setattr(settings, "follow_llm", "hcx")


@pytest.fixture(autouse=True)
def names(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_db():
        pytest.fail("대화 단위 테스트에서 실제 통계 DB를 호출했습니다")

    def resolve_region(name, year=None, sigungu=False):
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
    monkeypatch.setattr(lookup, "_table", lambda table: {})
    known = {"출생아 수", "사망자 수", "합계출산율", "고령인구 비율"}
    monkeypatch.setattr(indicators, "canonical_name", lambda n: n if n in known else None)
    aliases = {**dict.fromkeys(known), "65세 이상 비율": "고령인구 비율"}
    monkeypatch.setattr(
        indicators,
        "find_in_text",
        lambda t: next((real or n for n, real in aliases.items() if n in t), None),
    )
    monkeypatch.setattr(indicators, "specs", lambda: dict.fromkeys(known, {}))
    monkeypatch.setattr(
        indicators, "find_regions", lambda t: [r for r in ("서울", "부산") if r in t]
    )


def test_add_region_keeps_trend_for_each_region() -> None:
    c, changed = s.apply_changes(BASE, {"regions": {"op": "add", "value": "서울"}})
    assert c["regions"] == ["부산", "서울"] and c["type"] == "trend" and changed == {"regions"}
    assert (c["start_year"], c["end_year"]) == (2020, 2024)
    assert BASE["regions"] == ["부산"]
    again, _ = s.apply_changes(c, {"regions": {"op": "add", "value": ["서울특별시"]}})
    assert again["regions"] == ["부산", "서울"]


def test_set_and_remove_region() -> None:
    c, _ = s.apply_changes(BASE, {"regions": {"op": "set", "value": ["대구"]}})
    assert c["regions"] == ["대구"] and c["type"] == "trend"
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
    assert there["type"] == "value"


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
    p, src = s.inherit(value, plain, BASE, USER)
    assert p["regions"] == ["부산"] and p["year"] == 2025 and src["period"] == "default"


def test_no_inherit_for_said_conditions_rank_or_default_period() -> None:
    said = {**BASE, "regions": ["서울"]}
    src = {"indicator": "user", "regions": "user", "period": "user", "sex": "default"}
    assert s.inherit(said, src, BASE, USER)[0]["regions"] == ["서울"]
    rank = {**BASE, "type": "rank", "regions": []}
    plain = {"indicator": "user", "regions": "default", "period": "default", "sex": "default"}
    assert s.inherit(rank, plain, BASE, USER)[0]["regions"] == []
    prev_default = {**USER, "period": "default"}
    assert s.inherit({**BASE, "regions": []}, plain, BASE, prev_default)[1]["period"] == "default"


def test_conditions_keep_the_period_actually_answered() -> None:
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
    assert failed["start_year"] == 2019 and failed["asked"] == {"recent": 5}


def test_applied_shows_conditions_with_source() -> None:
    rows = s.applied(BASE, {**USER, "period": "inherited"})
    assert rows == [
        {"name": "지표", "value": "출생아 수", "source": "user"},
        {"name": "지역", "value": "부산", "source": "user"},
        {"name": "기간", "value": "2020~2024년", "source": "inherited"},
        {"name": "성별", "value": "계", "source": "default"},
    ]
    assert s.applied({"type": "other"}, {}) == []


def test_pick_region_reply() -> None:
    options = ["광주광역시", "경기도 광주시"]
    assert s.pick_region("광주광역시요", options) == "광주광역시"
    assert s.pick_region("경기도 광주시", options) == "경기도 광주시"
    assert s.pick_region("2번", options) == "경기도 광주시"
    assert s.pick_region("광주", options) is None
    assert s.pick_region("서울 인구 알려줘", options) is None
    c = s.replace_region({**BASE, "regions": ["서울", "광주"]}, "광주", "광주광역시")
    assert c["regions"] == ["서울", "광주광역시"]


@pytest.mark.parametrize(
    ("question", "view"),
    [
        ("막대그래프로 보여줘", "bar"),
        ("표로", "table"),
        ("꺾은선으로 바꿔줘", "line"),
        ("그래프로 보여줘", "chart"),
        ("차트로 그려줘", "chart"),
        ("시각화해줘", "chart"),
        ("막대그래프는?", "bar"),
        ("테이블로 보여줘", "table"),
        ("선으로 보여줘", "line"),
        ("원그래프로 보여줘", "pie"),
        ("표 말고 그래프로", "chart"),
        ("그래프 말고 표로 보여줘", "table"),
        ("이거 막대그래프로 바꿔서 보여줄 수 있어?", "bar"),
        ("부산 출생아 수 막대그래프로", None),
        ("2020년 막대그래프로 보여줘", None),
        ("막대한 영향이 있나요", None),
        ("서울도 막대로 보여줘", None),
        ("추이를 막대그래프로 보여줘", None),
        ("통계표 보여줘", None),
        # 요청 말 · 채움말만 더한 그림 바꾸기
        ("막대그래프로 다시 보여줘", "bar"),
        ("막대그래프로 좀 볼 수 있을까요?", "bar"),
        ("그래프 형태로 바꿔 주세요", "chart"),
        ("이번엔 표로", "table"),
        ("막대그래프가 보고 싶어", "bar"),
        ("그래프로 그려줄래?", "chart"),
        ("그림으로 보여줘", "chart"),
        # 조건이 남으면 조건 변경 (11/26 리뷰: 빼는 말 목록에 없는 조건은 버리고 그림만 바꿨다)
        ("남자만 막대그래프로 보여줘", None),
        ("여자는 그래프로", None),
        ("65세 이상만 막대로", None),
        ("내국인만 막대그래프로", None),
        ("상위 5개만 막대로", None),
        ("많은 순으로 막대그래프", None),
        ("10개만 막대그래프로 보여줘", None),
        ("비율로 막대그래프 보여줘", None),
        ("증가율로 그래프", None),
        ("평균으로 막대그래프", None),
        ("합계만 표로", None),
    ],
)
def test_chart_request(question, view) -> None:
    assert s.chart_request(question) == view


def test_chart_shape_is_read_with_a_condition_change() -> None:
    assert s.chart_shape("남자만 막대그래프로 보여줘") == "bar"
    assert s.chart_shape("65세 이상만 표로") == "table"
    assert s.chart_shape("남자만 보여줘") is None


def test_crowded_line_chart_is_not_said_to_be_bars() -> None:
    """지역이 6개를 넘는 추이는 화면이 막대 없이 지역별 꺾은선으로 그린다: 막대로 바꿨다고
    답하면 화면과 어긋났다 (11/26 리뷰)"""
    seven = {"type": "line", "rows": [], "series": [f"지역{i}" for i in range(7)]}
    chart, said = s.redraw(seven, "bar", "ok")
    assert chart["view"] == "line"
    assert said == (
        "7개 지역을 막대로 그리면 겹쳐 읽기 어렵습니다. 지역별 꺾은선 그래프로 보여 드립니다."
    )
    six = {**seven, "series": seven["series"][:6]}
    assert s.redraw(six, "bar", "ok") == ({**six, "view": "bar"}, "막대그래프로 바꿨습니다.")


@pytest.mark.parametrize(
    ("kind", "view", "shown", "text"),
    [
        ("line", "bar", "bar", "막대그래프로 바꿨습니다."),
        ("line", "table", "table", "표로 바꿨습니다."),
        ("line", "chart", "line", "꺾은선 그래프로 보여 드립니다."),
        ("bar", "bar", "bar", "막대그래프로 바꿨습니다."),
        ("bar", "line", "bar", "꺾은선은 연도별 추이에만 씁니다. 막대그래프로 보여 드립니다."),
        ("parts", "bar", None, "구성비 막대그래프로 바꿨습니다."),
        ("parts", "chart", None, "구성비 막대그래프로 보여 드립니다."),
        ("parts", "pie", None, "원그래프는 지원하지 않습니다. 구성비 막대그래프로 보여 드립니다."),
    ],
)
def test_redraw_shows_a_drawable_view(kind, view, shown, text) -> None:
    chart, said = s.redraw({"type": kind, "rows": [], "view": "table"}, view, "ok")
    assert chart["view"] == shown and said == text


def test_chart_view_and_summary() -> None:
    chart, said = s.redraw(None, "bar", "ok")
    assert chart is None and said.startswith("직전 답은 값이 하나라 그래프로 그릴 수 없습니다.")
    assert s.redraw(None, "chart", "tables") == (None, "직전 답에는 그래프로 그릴 수치가 없습니다.")
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


class Talk:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.store: dict = {}
        self.turns: list = []
        self.asked: list = []
        self.extracted: list = []
        self.answered: list = []
        self.delta: dict = {"request_type": "new", "changes": {}, "chart": None, "unclear": None}
        self.parsed: dict = dict(BASE)
        self.fail_when: object = lambda parsed: False
        self.searched: list = []
        self.found: dict = {"status": "ok", "answer": "인구총조사에서 확인할 수 있습니다."}
        self.at = s.now()
        self.sid: str | None = None
        monkeypatch.setattr(qa, "ask", self._ask)
        monkeypatch.setattr(qa, "answer", self._answer)
        monkeypatch.setattr(s, "extract", self._extract)
        monkeypatch.setattr(s, "load", self._load)
        monkeypatch.setattr(s, "save", self._save)
        monkeypatch.setattr(s, "turn_writer", memory_turn_writer(self.store))
        monkeypatch.setattr(s, "log_turn", self._log)
        monkeypatch.setattr(s, "now", lambda: self.at)
        monkeypatch.setattr(table_guide, "find_tables", self._find)
        monkeypatch.setattr(table_guide, "cards_for", self._cards)
        self.dynamic: dict | None = None
        monkeypatch.setattr(dynamic_indicator, "answer", self._dynamic)
        self.cached: dict | None = None
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

    async def _save(self, session_id, state, conn=None):
        self.store[session_id] = (state, self.at)

    async def _log(self, session_id, user_id, turn_no, question, res, before, after, conn=None):
        self.turns.append((turn_no, res.get("request_type"), question))

    user_id: int | None = 7

    def say(self, question: str, selections: dict | None = None, **delta) -> dict:
        self.delta = delta
        res = asyncio.run(s.handle(self.sid, question, user_id=self.user_id, selections=selections))
        self.sid = res["session_id"]
        return res


@pytest.fixture
def talk(monkeypatch: pytest.MonkeyPatch) -> Talk:
    return Talk(monkeypatch)


def test_conversation_deleted_while_answering_is_not_saved_again(
    talk: Talk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """이어 묻는 답을 만드는 사이 대화를 지우면 상태 · 기록 · 목록을 다시 만들지 않는다.
    그대로 저장하면 같은 ID로 다시 생겨 지운 대화가 되살아났다 (11/26 리뷰)"""
    talk.say("부산 출생아 최근 5년 보여 줘")
    touched: list[str] = []

    async def touch(session_id, user_id, question, conn=None):
        touched.append(question)

    monkeypatch.setattr(s.conversations, "touch", touch)
    turn = s._turn

    async def deleted_while_answering(*args, **kwargs):
        out = await turn(*args, **kwargs)
        talk.store.clear()  # conversations.delete가 상태까지 지운다
        return out

    monkeypatch.setattr(s, "_turn", deleted_while_answering)
    res = talk.say("서울은?")
    assert res["status"] == "ok"  # 보낸 요청에는 답한다
    assert talk.store == {} and touched == []
    assert [q for *_, q in talk.turns] == ["부산 출생아 최근 5년 보여 줘"]


def test_first_question_starts_a_session(talk: Talk) -> None:
    res = talk.say("부산 출생아 최근 5년 보여 줘")
    assert (
        res["status"] == "ok"
        and res["request_type"] == "new"
        and res["session_id"].startswith("A_")
    )
    assert res["chart"]["type"] == "line" and res["notice"] is None
    assert [a["value"] for a in res["applied"]] == ["출생아 수", "부산", "2020~2024년", "계"]
    assert talk.extracted == []
    assert talk.turns == [(1, "new", "부산 출생아 최근 5년 보여 줘")]


def test_change_recomputes_without_reparsing(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["request_type"] == "change" and res["parsed"]["type"] == "value"
    assert res["parsed"]["year"] == 2022 and res["parsed"]["regions"] == ["부산"]
    assert len(talk.asked) == 1 and len(talk.answered) == 1
    period = next(a for a in res["applied"] if a["name"] == "기간")
    assert period == {"name": "기간", "value": "2022년", "source": "user"}
    res = talk.say(
        "서울도 같이", request_type="change", changes={"regions": {"op": "add", "value": "서울"}}
    )
    assert res["parsed"]["regions"] == ["부산", "서울"] and res["parsed"]["type"] == "compare"
    assert "2022년 기준으로 비교" not in (res["notice"] or "")


def test_adding_region_to_trend_keeps_the_period(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    change = {"regions": {"op": "add", "value": "서울"}}
    res = talk.say("서울도 같이", request_type="change", changes=change)
    assert res["parsed"]["type"] == "trend" and res["parsed"]["regions"] == ["부산", "서울"]
    assert (res["parsed"]["start_year"], res["parsed"]["end_year"]) == (2020, 2024)
    assert res["notice"] is None


def test_chart_and_summary_reuse_last_answer(talk: Talk) -> None:
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("이 결과를 막대그래프로 변경해줘.")
    assert res["request_type"] == "chart" and res["chart"]["view"] == "bar"
    assert res["chart"]["rows"] == first["chart"]["rows"] and res["sources"] == first["sources"]
    assert talk.extracted == [] and talk.answered == []
    res = talk.say("요약해줘")
    headline = "부산의 출생아 수는 2020~2024년에 4명 증가했습니다(20명 → 24명)."
    assert res["request_type"] == "summary" and res["summary"] == first["summary"]
    assert res["answer"].split("\n")[0] == headline == first["summary"]["headline"]
    assert "\n\n" not in res["answer"]
    assert talk.say("한 줄로 요약해줘")["answer"] == headline
    assert talk.extracted == [] and talk.answered == []
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


def test_chart_of_a_single_value_says_why_instead_of_asking_back(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "start_year": None, "end_year": None}
    talk.say("2024년 부산 출생아 수")
    res = talk.say("막대그래프로 보여줘")
    assert res["request_type"] == "chart" and res["status"] == "ok" and res["chart"] is None
    assert res["answer"].startswith("직전 답은 값이 하나라 그래프로 그릴 수 없습니다.")
    assert talk.extracted == []
    assert talk.say("2022년만", request_type="change", changes={"year": 2022})["status"] == "ok"


@pytest.mark.parametrize(
    ("said", "changes"),
    [
        ("65세 이상만", {"ages": [[65, None]]}),
        ("65세 이상만 막대그래프로 보여줘", {"ages": [[65, None]]}),
        ("노인은?", {"ages": [[65, None]]}),
        ("그럼 30대는 어때?", {"ages": [[30, 39]]}),
        ("20대 여자만", {"ages": [[20, 29]], "sex": "여"}),
        ("서울 65세 이상만", None),  # 지역도 바꾼다: 변경 추출에
        ("2023년 65세 이상만", None),
        ("65세 이상 비율은?", None),  # 비율 후속 질문
        ("남자만", None),
    ],
)
def test_age_only_follow_up(said: str, changes: dict | None) -> None:
    """변경 추출 형식에 연령이 없어 '65세 이상만'을 HCX가 지표('고령인구')로 바꾸거나 새 질문으로
    읽어 앞 조건을 잃었다 (11/26 점검)"""
    assert s.age_only_changes(said, {"indicator": "총인구", "type": "trend"}) == changes


def test_age_change_is_applied_like_inheritance() -> None:
    before = {"indicator": "총인구", "term": "총인구", "type": "trend", "age_group": "고령"}
    after, changed = s.apply_changes(before, {"ages": [[65, None]]})
    assert after["ages"] == [[65, None]] and "age_group" not in after
    assert after["term"] == "총인구" and after["type"] == "trend" and changed == {"ages"}


def test_age_only_follow_up_keeps_conditions_without_llm(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("65세 이상만 막대그래프로 보여줘")
    assert res["request_type"] == "change" and talk.extracted == []
    asked = talk.answered[-1]
    assert asked["ages"] == [[65, None]] and asked["regions"] == ["부산"]
    assert asked["type"] == "trend" and res["chart"]["view"] == "bar"


def test_whole_talk_summary_request() -> None:
    for said in (
        "지금까지 분석한 내용을 요약해줘",
        "앞에서 본 거 정리해줘",
        "대화 내용 요약해 줘",
        # 범위를 가리키는 다른 말 (11/26 점검: 몇 낱말만 잡아 직전 답만 요약했다)
        "전체 내용 요약해줘",
        "다 정리해줘",
        "처음부터 요약해줘",
        "전부 요약해 줘",
        "모두 정리해줘",
        "물어본 것들 정리해줘",
        "이제까지 나온 결과 요약",
    ):
        assert s.whole_talk_summary_request(said), said
    for said in (
        "요약해줘",
        "다시 요약해줘",
        "한 줄로 요약해줘",
        "전체적으로 요약해줘",
        "서울 전체 인구 요약 통계 알려줘",
        "지금까지 서울 인구 정리해줘",
    ):
        assert not s.whole_talk_summary_request(said), said


def test_whole_talk_summary_lists_every_answer(talk: Talk) -> None:
    """'지금까지 분석한 내용을 요약해줘'가 요약으로 분류돼도 직전 답만 요약했다 (11/26 리뷰)"""
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    second = talk.say("서울은?")
    talk.say("막대그래프로 보여줘")  # 차트만 바꾼 턴은 새 답이 아니다
    res = talk.say("지금까지 분석한 내용을 요약해줘")
    assert res["request_type"] == "summary" and talk.extracted == []
    lines = res["answer"].split("\n")
    assert lines[0] == "지금까지 본 내용 2가지입니다."
    assert lines[1:] == [f"- {first['summary']['headline']}", f"- {second['summary']['headline']}"]


@pytest.mark.parametrize(
    ("said", "kind", "expected"),
    [
        ("최근 5년간 추이를 보여줘", "value", True),
        ("그럼 지난 10년 변화는?", "trend", True),
        ("최근 3년은 어때?", "compare", True),
        ("최근 5년간 추이를 보여줘", "rank", False),
        ("최근 5년 서울은?", "value", False),
    ],
)
def test_recent_trend_reply(said: str, kind: str, expected: bool) -> None:
    assert s.recent_trend_reply(said, {"type": kind}) is expected


def test_recent_trend_follow_up_does_not_depend_on_llm(talk: Talk) -> None:
    """'최근 5년간 추이를 보여줘'는 변경 추출(HCX) 분류에 따라 결과가 달랐다 (11/26 리뷰)"""
    talk.parsed = {**BASE, "type": "value", "year": 2024, "start_year": None, "end_year": None}
    talk.say("2024년 부산 출생아 수")
    # 가짜 계산은 N년을 연도로 풀지 못한다: 조건만 본다
    talk.fail_when = lambda p: bool((p.get("asked") or {}).get("recent"))
    res = talk.say("최근 5년간 추이를 보여줘")
    assert res["request_type"] == "change" and talk.extracted == []
    asked = talk.answered[-1]
    assert asked["asked"]["recent"] == 5 and asked["type"] == "trend"
    assert asked["regions"] == ["부산"]


def test_chart_with_a_sex_change_recomputes_and_keeps_the_shape(talk: Talk) -> None:
    """'남자만 막대그래프로'가 성별을 다시 계산하지 않고 전체 값의 차트 모양만 바꿨다
    (11/26 리뷰)"""
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("남자만 막대그래프로 보여줘", request_type="change", changes={"sex": "남"})
    # 변경 추출에는 조건 부분만: 차트 말이 섞이면 HCX가 '그림 바꾸기'로 읽어 조건을 버렸다
    assert talk.extracted == ["남자만 보여줘"]
    assert res["request_type"] == "change" and talk.answered[-1]["sex"] == "남"
    assert res["chart"]["view"] == "bar"
    # 질문의 모양 말이 '차트'뿐이면 변경 추출이 읽은 모양
    res = talk.say("여자만 바 차트로", request_type="change", changes={"sex": "여"}, chart="bar")
    assert talk.answered[-1]["sex"] == "여" and res["chart"]["view"] == "bar"


def test_new_question_with_a_chart_shape_keeps_the_shape(talk: Talk) -> None:
    """차트 말과 함께 새 질문을 하면 새 질문 경로가 말한 모양을 버렸다 (11/26 점검)"""
    first = talk.say("부산 출생아 최근 5년 막대그래프로 보여 줘")  # 첫 질문
    assert first["request_type"] == "new" and first["chart"]["view"] == "bar"
    res = talk.say("대구 출생아 수 최근 5년 막대그래프로", request_type="new")  # 이어 묻는 새 질문
    assert res["request_type"] == "new" and res["chart"]["view"] == "bar"
    plain = talk.say("부산 출생아 최근 5년 보여 줘", request_type="new")
    assert plain["chart"].get("view") is None  # 모양을 말하지 않으면 기본 차트 그대로


@pytest.mark.parametrize(
    ("said", "condition"),
    [
        ("남자만 막대그래프로 보여줘", "남자만 보여줘"),
        ("10개만 막대로 보여줘", "10개만 보여줘"),
        ("여자만 표로 바꿔줘", "여자만 바꿔줘"),
        ("부산도 같이 꺾은선으로", "부산도 같이"),
        ("2023년만 그래프 형태로 보여줘", "2023년만 보여줘"),
        ("여성 인구 표 말고 그래프로", "여성 인구"),
    ],
)
def test_condition_part_drops_only_the_chart_words(said: str, condition: str) -> None:
    assert s.condition_part(said) == condition


def test_parse_delta_moves_a_chart_shape_out_of_the_changes() -> None:
    """HCX가 그림 모양을 조건에 넣었다 ("type": "table"). 맞지 않는 type이 남으면 보는 방식을
    다시 맞추는 단계를 건너뛴다 (11/26 점검)"""
    raw = '{"request_type": "change", "changes": {"sex": "여", "type": "table"}}'
    assert s.parse_delta(raw) == {
        "request_type": "change",
        "changes": {"sex": "여"},
        "chart": "table",
        "unclear": None,
    }
    only_shape = s.parse_delta('{"request_type": "change", "changes": {"chart": "bar"}}')
    assert only_shape["request_type"] == "chart" and only_shape["chart"] == "bar"
    kept = s.parse_delta('{"request_type": "change", "changes": {"type": "trend"}}')
    assert kept["changes"] == {"type": "trend"} and kept["chart"] is None


def test_chart_without_a_shape_uses_the_last_chart(talk: Talk) -> None:
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("그래프로 보여줘")
    assert res["request_type"] == "chart" and res["chart"]["view"] == "line"
    assert res["chart"]["rows"] == first["chart"]["rows"] and talk.extracted == []
    res = talk.say("그림 좀 바꿔봐", request_type="chart", chart=None)
    assert talk.extracted == ["그림 좀 바꿔봐"]
    assert res["request_type"] == "chart" and res["chart"]["rows"] == first["chart"]["rows"]
    assert res["answer"] == "꺾은선 그래프로 보여 드립니다."


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
    assert res["parsed"]["regions"] == ["부산"]
    assert "기본 기간으로 답했습니다" in res["notice"] and "지역 부산" in res["notice"]


def test_year_reply_is_merged_into_asked_question(talk: Talk) -> None:
    first = talk.say("과거와 최근 인구수 비교해줘")
    assert first["status"] == "missing_year"
    res = talk.say("2000년")
    assert res["request_type"] == "reply" and res["question"] == "2000년과 최근 인구수 비교해줘"
    assert res["status"] == "ok" and talk.extracted == []
    talk.say("과거와 최근 인구수 비교해줘")
    res = talk.say("2024년 서울 고령인구 비율 알려줘", request_type="new")
    assert res["request_type"] == "new" and res["question"] == "2024년 서울 고령인구 비율 알려줘"


def test_region_reply_replaces_only_the_asked_region(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "compare", "regions": ["서울", "광주"]}
    first = talk.say("2024년 서울 광주 출생아 수 비교")
    assert first["status"] == "ambiguous_region"
    res = talk.say("광주광역시요")
    assert res["request_type"] == "reply" and res["parsed"]["regions"] == ["서울", "광주광역시"]
    assert talk.extracted == [] and len(talk.asked) == 1


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
    assert talk.extracted == []
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


@pytest.mark.parametrize("other", [7, 8, None])
def test_session_without_owner_metadata_starts_a_new_conversation(talk: Talk, other):
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    sid = first["session_id"]
    old_state, at = talk.store[sid]
    old_state.pop("user_id")
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
    talk.user_id = other
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["session_id"] != first["session_id"]
    assert res["request_type"] == "new" and talk.extracted == []
    assert "새 대화로 시작했습니다" in res["notice"]
    assert talk.store[first["session_id"]][0]["user_id"] == 7
    talk.user_id, talk.sid = 7, first["session_id"]
    again = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert again["session_id"] == first["session_id"] and again["request_type"] == "change"


def test_guest_session_continues_after_login(talk: Talk) -> None:
    talk.user_id = None
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    assert talk.store[first["session_id"]][0]["user_id"] is None
    talk.user_id = 7
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


def test_numeric_answer_carries_its_table_card(talk: Talk) -> None:
    res = talk.say("부산 출생아 최근 5년 보여 줘")
    assert res["status"] == "ok" and res["chart"]["type"] == "line"
    assert res["tables"] == {"status": "ok", "recommendations": [{"tbl_id": "T", "basis": "기준"}]}
    assert talk.searched == []


def test_unsupported_indicator_falls_back_to_table_search(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    res = talk.say("반려동물 키우는 집 몇 곳이야?")
    assert res["status"] == "tables" and res["chart"] is None and res["applied"] == []
    assert res["tables"]["recommendations"] == [{"tbl_id": "DT_X"}]
    assert "수치를 바로 계산하지 못해" in res["answer"]
    question, search_session, _ = talk.searched[0]
    assert question == "반려동물 키우는 집 몇 곳이야?" and search_session.startswith("S_")
    talk.parsed = dict(BASE)
    assert talk.say("부산 출생아 최근 5년")["request_type"] == "new" and talk.extracted == []


def test_unsupported_indicator_computed_from_found_table(talk: Talk) -> None:
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
    saved = talk.store[res["session_id"]][0]["conditions"]
    assert saved["indicator"] == "1인 가구 수" and saved["dynamic_spec"] == spec


def test_cached_plan_answers_without_table_search(talk: Talk) -> None:
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
    talk.parsed = {**BASE, "type": "value", "indicator": "출생아 수", "grounded": False}
    talk.fail_when = lambda parsed: True
    res = talk.say("2020년 서울 1인 가구 수")
    assert res["status"] == "tables" and res["trace"]["replaced"]["by"] == "tables"


def test_ungrounded_dictionary_answer_not_returned_when_dynamic_fails(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "indicator": "출생아 수", "grounded": False}
    res = talk.say("서울 1인 가구 중 여성 비율")
    assert res["status"] == "tables" and res["tables"]["recommendations"] == [{"tbl_id": "DT_X"}]
    assert res["trace"]["replaced"]["indicator"] == "출생아 수"
    talk.parsed = {**BASE, "type": "value", "indicator": "출생아 수", "grounded": True}
    searched = len(talk.searched)
    talk.say("부산 출생아 수")
    assert len(talk.searched) == searched


@pytest.mark.parametrize("status", ["out_of_scope", "partial_match"])
def test_also_asked_failure_keeps_dictionary_answer(talk: Talk, status: str) -> None:
    talk.parsed = {
        **BASE,
        "type": "value",
        "indicator": "출생아 수",
        "grounded": False,
        "also_asked": "1인 가구 수",
    }
    talk.found = {"status": status, "answer": "표 안내"}
    res = talk.say("2020년 서울 출생아 수랑 1인 가구 수 알려줘")
    assert res["status"] == "ok" and res["result"] == {"status": "ok", "value": 1}
    assert [q for q, *_ in talk.searched] == ["2020년 서울 1인 가구 수 알려줘"]
    if status == "out_of_scope":
        assert "'1인 가구 수'는 맞는 통계표를 찾지 못해" in res["answer"]
    else:
        assert "'DT_X' 표에서 확인할 수 있습니다" in res["answer"]


def test_table_cell_clarification_then_pick(talk: Talk, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert len(talk.asked) == asked


@pytest.mark.parametrize("refused", ["out_of_scope", "not_statistics"])
def test_out_of_scope_is_refused_not_table_guide(talk: Talk, refused: str) -> None:
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    talk.found = {"status": refused, "answer": "제공 범위에서 찾지 못했습니다."}
    res = talk.say("서울 반려동물 몇 마리야")
    assert res["status"] == refused and res["chart"] is None
    assert "수치를 바로 계산하지 못해" not in res["answer"]
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
    assert len(talk.asked) == 1


def test_chart_change_does_not_search_again(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("표로 보여줘")
    assert res["request_type"] == "chart" and "tables" not in res and talk.searched == []


def test_new_question_without_indicator_inherits_it() -> None:
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
    guessed = {**parsed, "indicator": "총인구", "term": None}
    p3, _ = s.inherit(guessed, s.sources_of(question, guessed), prev, prev_sources, question)
    assert p3["indicator"] == "고령인구 비율"


@pytest.mark.parametrize(
    ("question", "year"),
    [
        ("그 전년도는?", 2019),
        ("전년도는?", 2019),
        ("그 다음 해는?", 2021),
        ("이듬해는?", 2021),
        ("작년은?", 2025),
        ("올해는?", 2026),
        ("재작년은?", 2024),
        ("3년 전은?", 2023),
        ("전년 대비 얼마나 늘었어?", None),
        ("2018년은?", None),
        ("작년부터 보여줘", None),
        ("부산은?", None),
        ("작년 인구가 2000명 이하인 곳은?", 2025),
        ("작년 인구가 2000 명 이하인 곳은?", 2025),
    ],
)
def test_follow_year(question, year) -> None:
    from datetime import date

    value = {**BASE, "type": "value", "year": 2020, "start_year": None, "end_year": None}
    assert s.follow_year(question, value, today=date(2026, 11, 12)) == year


def test_relative_year_overrides_hcx(talk: Talk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s, "follow_year", lambda q, c, today=None: 2025 if "작년" in q else None)
    talk.say("2020년 부산 출생아 수")
    res = talk.say("작년은?", request_type="change", changes={"year": 2021})
    assert res["parsed"]["year"] == 2025 and res["request_type"] == "change"


def test_relative_year_keeps_hcx_new_question(talk: Talk, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s, "follow_year", lambda q, c, today=None: 2025 if "작년" in q else None)
    talk.say("2020년 부산 출생아 수")
    res = talk.say("작년 대구에서 새로 생긴 아기네 집 몇 곳이야?", request_type="new")
    assert res["request_type"] == "new"


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
    assert trace["understand_max_completion_tokens"] == qa.UNDERSTAND_MAX_TOKENS == 1024
    assert sent["maxCompletionTokens"] == 1024 and sent["thinking"] == {"effort": "none"}


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
    talk.parsed = {**BASE, "indicator": "순이동", "regions": ["부산"]}
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
    talk.say("부산에서 이사한 건수")
    assert talk.say("응")["status"] == "clarify_question"
    res = talk.say("모르겠어")
    assert res["status"] == "ok" and res["parsed"]["indicator"] == "전출자 수"
    assert "알아듣지 못해" in res["notice"]
    assert talk.store[talk.sid][0]["pending"] is None
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
    talk.say("2024년 우리 동네 출생아 수")
    res = talk.say("고성")
    assert res["status"] == "clarify_question"
    assert {"강원특별자치도 고성군", "경상남도 고성군"} <= set(res["clarification"]["options"])


def test_local_region_reply_with_comma_and_sigungu_first(
    talk: Talk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """변형셋 P-121: '영도구, 부산이요'를 지역 답으로 못 읽어 같은 되묻기를 되풀이했다 (11/25)"""
    monkeypatch.setattr(
        indicators, "find_regions", lambda t: [r for r in ("영도구", "부산") if r in t]
    )
    monkeypatch.setattr(
        indicators,
        "resolve_region",
        lambda name, year=None, sigungu=False: (
            {"status": "ok", "display": "부산광역시 영도구"}
            if name == "부산 영도구"
            else {"status": "unknown_region", "region": name}
        ),
    )
    assert talk.say("요새 우리 동네 사람 줄어드는 중이야?")["status"] == "clarify_question"
    talk.parsed = {**BASE, "regions": ["부산 영도구"]}
    res = talk.say("영도구, 부산이요")
    assert res["request_type"] == "reply" and res["status"] == "ok"
    assert talk.asked[-1] == "요새 부산 영도구 사람 줄어드는 중이야?"


def test_follow_up_rules_for_region_order_scope_and_period() -> None:
    """후속 질문이 새 질문(new)으로 읽혀 앞 조건을 잃던 것 (11/25 멀티턴 평가 조건 변경)"""
    value = {**BASE, "type": "value", "start_year": None, "end_year": None}
    rank = {**BASE, "type": "rank", "regions": [], "start_year": None, "end_year": None}
    assert s.region_only_changes("그럼 부산은 어때?", value) == {
        "regions": {"op": "set", "value": ["부산"]}
    }
    assert s.region_only_changes("그러면 서울도?", value)["regions"]["op"] == "add"
    assert s.rank_scope_changes("서울 안에서는?", rank) == {"sido": "서울", "level": "시군구"}
    assert s.rank_scope_changes("그럼 부산 내에서 순위는?", rank)["sido"] == "부산"
    assert s.rank_scope_changes("서울 안에서는?", value) is None
    assert s.rank_scope_changes("그 안에서는?", rank) is None
    assert s.order_changes("낮은 순으로", rank) == {"ascending": True}
    assert s.order_changes("그럼 많은 순서로 보여줘", rank) == {"ascending": False}
    assert s.order_changes("낮은 순으로", value) is None
    for said, n in (
        ("10년으로 늘려줘", 10),
        ("기간을 3년으로", 3),
        ("그럼 5년으로 줄여줘", 5),
        ("7년치로", 7),
    ):
        assert s.period_length_reply(said) and s.recent_years(said) == n, said
    # 기간을 말하는 낱말이 없으면 줄여 말한 연도일 수 있다 ('24년으로 바꿔줘'는 2024년)
    for said in (
        "2015년으로",
        "10년 전은?",
        "3년으로 나눠서 보여줘",
        "24년으로 바꿔줘",
        "그럼 20년으로 보여줘",
        "그럼 7년으로",
    ):
        assert not s.period_length_reply(said), said
        assert s.recent_years(said) is None, said
    for said, word in (("혼인도 알려줘", "혼인"), ("그럼 이혼은?", "이혼"), ("사망자도", "사망자")):
        assert s.said_other_subject(said) == word, said
    for said in (
        "부산은?",
        "작년은?",
        "남자는?",
        "노인은?",
        "추이는?",
        "비율은?",
        "거기는?",
        "요약해줘",
        "서울 혼인 건수는?",
        "전국은?",
        "월별은?",
        "여아는?",
        "상위는?",
        "몇명이야?",
        "그럼 얼마야?",
        "어때?",
        "몇건이야?",
    ):
        assert s.said_other_subject(said) is None, said
    assert not s.names_indicator("부산은?", {"indicator": "출생아 수"})


def test_follow_up_naming_another_word_does_not_inherit_the_indicator(talk: Talk) -> None:
    """출생아 수 뒤 '사망자도 알려줘': HCX가 지표를 비우면 앞 지표 출생아 수로 답했다 (11/26)"""
    talk.say("부산 출생아 최근 5년 보여 줘")
    talk.parsed = {**BASE, "indicator": None, "term": None}
    res = talk.say("혼인도 알려줘")
    assert res["parsed"].get("indicator") is None and res["parsed"]["term"] == "혼인"
    talk.parsed = dict(BASE)
    talk.say("부산 출생아 최근 5년 보여 줘")
    talk.parsed = {**BASE, "indicator": None, "term": None}
    res = talk.say("2020년부터 보여줘")
    assert res["parsed"]["indicator"] == "출생아 수"


def test_rank_follow_ups_change_scope_and_order_without_llm(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "rank", "regions": [], "start_year": None, "end_year": None}
    talk.say("2024년 시도별 출생아 수 순위")
    res = talk.say("서울 안에서는?")
    assert res["request_type"] == "change" and talk.extracted == []
    assert (talk.answered[-1]["sido"], talk.answered[-1]["level"]) == ("서울", "시군구")
    talk.say("낮은 순으로")
    assert talk.extracted == [] and talk.answered[-1]["ascending"] is True
    assert talk.answered[-1]["sido"] == "서울" and talk.answered[-1].get("top") is None


def test_period_length_follow_up_keeps_conditions_without_llm(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    talk.fail_when = lambda p: bool((p.get("asked") or {}).get("recent"))
    res = talk.say("10년으로 늘려줘")
    assert res["request_type"] == "change" and talk.extracted == []
    asked = talk.answered[-1]
    assert asked["asked"]["recent"] == 10 and asked["regions"] == ["부산"]
    assert asked["indicator"] == "출생아 수" and asked["type"] == "trend"


def test_short_year_follow_up_is_not_read_as_period_length(talk: Talk) -> None:
    """'24년으로 바꿔줘'는 2024년: 기간 길이 규칙이 24년치 추이로 바꾸거나
    변경 추출이 읽은 연도를 덮지 않는다 (11/26 리뷰)"""
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("24년으로 바꿔줘", request_type="change", changes={"year": 2024})
    assert res["request_type"] == "change" and talk.extracted
    asked = talk.answered[-1]
    assert not (asked.get("asked") or {}).get("recent")
    assert 2024 in (asked.get("year"), asked.get("end_year"))


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
    assert talk.extracted == [] and talk.answered == []
    assert not s.detail_request("서울 출생아 수 자세히 알려줘")
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["parsed"]["regions"] == ["부산"]


def test_summary_with_a_change_answers_the_changed_conditions(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("2022년만 요약해줘", request_type="summary", changes={"year": 2022})
    assert res["request_type"] == "change" and res["parsed"]["year"] == 2022
    assert len(talk.answered) == 1


def test_summary_with_nothing_to_summarize_is_not_read_as_a_question(talk: Talk) -> None:
    res = talk.say("요약해줘")
    assert res["status"] == "unclear" and res["request_type"] == "unclear"
    assert res["answer"].startswith("요약할 답이 아직 없습니다.") and res["summary"] is None
    assert talk.asked == [] and talk.extracted == []
    res = talk.say("부산 출생아 최근 5년 보여 줘")
    assert res["status"] == "ok" and res["request_type"] == "new"


def test_summary_after_tables_only_answer_repeats_its_lead(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "indicator": "반려동물 수"}
    first = talk.say("반려동물 키우는 집 몇 곳이야?")
    assert first["status"] == "tables"
    asked = len(talk.asked)
    res = talk.say("요약해줘")
    assert res["request_type"] == "summary" and res["summary"] is None
    assert res["answer"] == first["answer"].split("\n\n")[0]
    assert len(talk.asked) == asked and len(talk.searched) == 1


def test_bare_indicator_question_keeps_the_view_and_period() -> None:
    prev = {**BASE, "regions": ["부산", "서울"]}
    new = {"type": "value", "indicator": "사망자 수", "regions": [], "year": 2025, "sex": "계"}
    plain = {"indicator": "user", "regions": "default", "period": "default", "sex": "default"}
    p, src = s.inherit(new, plain, prev, USER, "사망자 수는?")
    assert p["type"] == "trend" and p["regions"] == ["부산", "서울"]
    assert (p["start_year"], p["end_year"]) == (2020, 2024) and p["indicator"] == "사망자 수"
    assert src["regions"] == "inherited" and src["period"] == "inherited"
    p, src = s.inherit(new, plain, prev, USER, "사망자 수가 가장 많은 곳은?")
    assert p["type"] == "compare" and p["year"] == 2025 and src["period"] == "default"


def test_value_question_with_several_inherited_regions_compares_them() -> None:
    prev = {**BASE, "regions": ["부산", "서울"]}
    new = {"type": "value", "indicator": "사망자 수", "regions": [], "year": 2022, "sex": "계"}
    said = {"indicator": "user", "regions": "default", "period": "user", "sex": "default"}
    p, src = s.inherit(new, said, prev, USER, "2022년 사망자 수는?")
    assert p["type"] == "compare" and p["regions"] == ["부산", "서울"] and p["year"] == 2022
    one = s.inherit(new, said, BASE, USER, "2022년 사망자 수는?")[0]
    assert one["type"] == "value" and one["regions"] == ["부산"]


def test_greeting_is_answered_without_hcx_and_keeps_conditions(talk: Talk) -> None:
    res = talk.say("안녕하세요")
    assert res["status"] == "chat" and res["request_type"] == "chat" and res["sources"] == []
    talk.say("부산 출생아 최근 5년 보여 줘")
    for said in ("고마워", "감사합니다!", "네", "ㅇㅋ"):
        res = talk.say(said)
        assert res["request_type"] == "chat" and "이어서 질문해 주세요" in res["answer"], said
        assert [a["value"] for a in res["applied"]][:2] == ["출생아 수", "부산"]
    assert len(talk.asked) == 1 and talk.extracted == [] and talk.searched == []
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert res["parsed"]["regions"] == ["부산"]
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
    assert not s.source_request("출생아 수 출처 통계표 찾아줘")


def test_why_question_says_no_cause_and_repeats_the_summary(talk: Talk) -> None:
    first = talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("왜 늘어난 거야?")
    assert res["request_type"] == "summary" and talk.extracted == []
    assert res["answer"].split("\n")[:2] == [s.NO_CAUSE, first["summary"]["headline"]]
    assert not s.why_request("왜 서울 출생아 수가 줄었어?")


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
    assert not s.composition_request("2022년 구성비로 보여줘")
    assert not s.composition_request("출생아 수 구성비 알려줘")


NOT_ABOUT_LAST = [
    "다문화 관련 어떤 통계가 있어?",
    "이민자는 어떤 자료가 있어?",
    "어느 통계표를 보면 돼?",
    "외국인은 무슨 조사로 알 수 있어?",
    "왜관읍은?",
    "칠곡군 왜관읍도 같이",
    "고령화 영향 큰 곳은?",
    "정책 효과 있는 지역은?",
    "구별로 자세히 보여줘",
    "연령별로 자세하게",
    "남녀로 나눠서 구체적으로",
    "서울은 왜 줄었어?",
    "2022년은 출처가 뭐야?",
    "부산만 자세히",
]


@pytest.mark.parametrize("said", NOT_ABOUT_LAST)
def test_rules_leave_other_meanings_to_hcx(talk: Talk, said: str) -> None:
    assert not (s.detail_request(said) or s.source_request(said) or s.why_request(said)), said
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say(said)
    assert res["request_type"] not in ("detail", "source", "summary"), said
    if said == "구별로 자세히 보여줘":
        assert res["request_type"] == "change" and res["parsed"]["sido"] == "부산"
        assert res["parsed"]["level"] == "시군구" and len(talk.answered) == 1
    elif res["status"] == "tables":
        assert [q for q, *_ in talk.searched] == [said]
    else:
        assert talk.extracted == [said]


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
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say(said)
    assert res["status"] == "tables" and res["request_type"] == "new" and res["applied"] == []
    assert res["tables"]["recommendations"] == [{"tbl_id": "DT_X"}]
    assert [q for q, *_ in talk.searched] == [said] and len(talk.asked) == 1
    assert not s.table_question("이 답은 어느 통계표에서 나온 거야?")
    assert not s.table_question("출생아 수 통계표 찾아줘")


def test_gu_ranking_stays_in_the_province_being_viewed() -> None:
    rank = {**BASE, "type": "rank", "regions": [], "level": "시군구", "sido": "서울"}
    plain = {"indicator": "user", "regions": "default", "period": "default", "sex": "default"}
    p, src = s.inherit(rank, plain, BASE, USER, "구별로 보여줘")
    assert p["sido"] == "부산" and src["regions"] == "inherited"
    said = s.inherit(rank, plain, BASE, USER, "서울 구별로 보여줘")[0]
    assert said["sido"] == "서울"
    two = {**BASE, "regions": ["부산", "서울"]}
    assert s.inherit(rank, plain, two, USER, "구별로 보여줘")[0]["sido"] is None


def test_by_district_request_ranks_inside_the_province_being_viewed(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("구별로 자세히 보여줘")
    assert res["request_type"] == "change" and talk.extracted == [] and len(talk.asked) == 1
    p = res["parsed"]
    assert (p["type"], p["level"], p["sido"], p["regions"]) == ("rank", "시군구", "부산", [])
    assert p["indicator"] == "출생아 수"
    res = talk.say("시도별로 보여줘")
    assert (res["parsed"]["level"], res["parsed"]["sido"]) == ("시도", None)
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
    talk.parsed = {**BASE, "type": "value", "indicator": "이혼 건수"}
    talk.fail_when = lambda parsed: True

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
    res = talk.say(reply, selections=selections)
    assert res["status"] == "ok" and res["request_type"] == "reply"
    assert res["result"]["value"] == value and res["answer"].startswith(f"{value}년 답")
    assert len(talk.asked) == asked


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
    talk.fail_when = lambda parsed: True
    talk.dynamic = _computed(42)
    res = talk.say("부산 출생아 2030년")
    assert res["status"] == "ok" and res["result"]["value"] == 42
    assert res["request_type"] == "new" and talk.searched[-1][0] == "부산 출생아 2030년"


def test_dictionary_notice_is_kept_when_other_tables_do_not_compute(talk: Talk) -> None:
    talk.fail_when = lambda parsed: True
    talk.dynamic = None
    res = talk.say("부산 출생아 2030년")
    assert res["status"] == "out_of_range"
    assert res["tables"]["recommendations"] == [{"tbl_id": "DT_X"}]


def _handoff(review: str | None, year: bool) -> dict:
    cand = {"tbl_id": "DT_X", "checks": {"year": year}, "review": review and {"match": review}}
    return {"targets": [{"candidates": [cand]}]}


def test_out_of_range_becomes_table_guide_when_picked_table_has_the_year(talk: Talk) -> None:
    talk.parsed = {**BASE, "type": "value", "indicator": "혼인 건수"}
    talk.fail_when = lambda parsed: True
    original = talk._result

    def old(parsed, question=""):
        result = original(parsed, question)
        result["result"].update(available_years=[1990, 2025], asked_year=1977)
        return result

    talk._result = old
    talk.dynamic = None
    talk.found = {**talk.found, "handoff": _handoff("exact", True)}
    res = talk.say("1977년 대구 혼인 건수")
    assert res["status"] == "tables"
    assert res["answer"].startswith("기본 통계의 혼인 건수 자료는 1990~2025년만 있지만, 1977년")
    assert "다시 질문해 주세요" not in res["answer"]
    assert res["tables"]["recommendations"] == [{"tbl_id": "DT_X"}]
    assert res["trace"]["replaced"]["reason"] == "out_of_range"
    for handoff in (_handoff("exact", False), _handoff(None, True)):
        talk.found = {**talk.found, "handoff": handoff}
        assert talk.say("1977년 대구 혼인 건수")["status"] == "out_of_range"


def test_region_not_in_year_is_not_turned_into_table_guide() -> None:
    res = {"status": "region_not_in_year", "result": {}}
    found = {"status": "ok", "handoff": _handoff("exact", True)}
    assert s._year_found_elsewhere(res, found) is None
    assert s._year_found_elsewhere({**res, "status": "out_of_range"}, found)


def test_projection_suggestion_and_follow_ups_are_not_searched(talk: Talk, monkeypatch) -> None:
    talk.fail_when = lambda parsed: True
    talk.dynamic = _computed(42)
    res = {
        "status": "out_of_range",
        "request_type": "new",
        "result": {"suggestion": {"status": "ok"}},
    }
    assert not s.searchable_after(res)
    assert s.searchable_after({**res, "result": {}})
    assert not s.searchable_after({**res, "result": {}, "request_type": "change"})
    for status in ("missing_region", "unknown_region", "data_loading", "error", "ambiguous_region"):
        assert not s.searchable_after({**res, "result": {}, "status": status}), status


def test_dynamic_picking_dictionary_table_keeps_dictionary_answer(monkeypatch) -> None:
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
    res = {"status": "missing_region", "request_type": s.NEW, "parsed": {"grounded": False}}
    assert s.ungrounded(res) is True
    res["parsed"]["grounded"] = True
    assert s.ungrounded(res) is False


def test_reply_turn_can_search_other_tables() -> None:
    res = {"status": "missing_data", "request_type": s.REPLY, "result": {}}
    assert s.searchable_after(res) is True
    assert s.searchable_after({**res, "request_type": s.CHART}) is False


def test_alternative_picked_by_its_words() -> None:
    answers = [{"answer": "2024년 부산의 값은 10명입니다.", "result": {"notes": ["귀화자 포함"]}}]
    assert s.alternative_by_words("귀화자까지 포함으로요", answers) == "0"
    assert s.alternative_by_words("응 그걸로", answers) == "0"
    assert s.alternative_by_words("서울은?", answers) is None
    two = [*answers, {"answer": "다른 값", "result": {}}]
    assert s.alternative_by_words("응", two) is None


def test_after_table_choice_computes_with_original_question(monkeypatch) -> None:
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


def test_turn_records_flow_with_final_answer(talk: Talk) -> None:
    res = talk.say("부산 출생아 최근 5년 보여 줘")
    last = res["trace"]["flow"][-1]
    assert last["stage"] == "최종 답" and last["result"] == res["status"]
    assert last["detail"].startswith("new · ")


ONE_PERSON = {**BASE, "type": "value", "indicator": "1인 가구", "term": "1인 가구",
              "regions": ["서울"], "year": 2020, "start_year": None, "end_year": None}  # fmt: skip
RATIO_SPEC = {"name": "1인 가구 중 70세 이상 비율", "unit": "%", "kind": "ratio", "dynamic": True}


@pytest.mark.parametrize("followup", ["부산은?", "부산은 어때?"])
def test_household_age_ratio_keeps_context_after_only_guiding_to_source(
    talk, monkeypatch, followup
):
    table = {"tbl_nm": "성 및 연령별 1인가구 - 시군구", "start_year": 2015, "end_year": 2025}
    monkeypatch.setattr(lookup, "_table", lambda tbl_id: table)
    calls = []

    async def try_table(tbl_id, org_id, question, parsed, trace, forced, slots):
        calls.append((tbl_id, question))
        if parsed["regions"] == ["서울"]:
            return None, None
        result = talk._result({**parsed, "dynamic_spec": RATIO_SPEC}, question)
        result["result"].update(unit="%", tbl_id=tbl_id)
        return result, None

    monkeypatch.setattr(dynamic_indicator, "_try", try_table)
    first = "2020년 서울 1인 가구 중 70세 이상 비율은?"
    client = TestClient(main.app)
    guide = client.post("/ask", json={"question": first}).json()
    assert guide["status"] == "tables"
    assert guide["tables"]["recommendations"][0]["tbl_id"] == "DT_1PL1502"
    response = client.post(
        "/ask", json={"question": followup, "session_id": guide["session_id"]}
    ).json()
    assert response["status"] == "ok" and response["request_type"] == s.CHANGE
    assert response["parsed"]["ages"] == [[70, None]]
    assert response["parsed"]["regions"] == ["부산"] and response["parsed"]["year"] == 2020
    assert response["result"]["unit"] == "%"
    assert calls == [
        ("DT_1PL1502", "2020년 서울 1인 가구 중 70세 이상 비율"),
        ("DT_1PL1502", "2020년 부산 1인 가구 중 70세 이상 비율"),
    ]
    assert talk.asked == talk.extracted == talk.searched == []


@pytest.mark.parametrize(
    ("question", "part"),
    [
        ("그중 여성 비율은?", "여성 비율은?"),
        ("그중 여성 인구 비율은?", "여성 비율은?"),
        ("그중 여성 인구는 몇 퍼센트야?", "여성 몇 퍼센트야?"),
        ("그중 일흔 살 이상 여성 인구 비율은?", "70세 이상 여성 비율은?"),
    ],
)
def test_ratio_follow_up_is_asked_with_last_conditions(
    talk: Talk, question: str, part: str
) -> None:
    talk.parsed = dict(ONE_PERSON)
    talk.say("2020년 서울 1인 가구 수는?")
    res = talk.say(question, request_type="change", changes={"sex": "여"})
    assert talk.extracted == []
    assert talk.asked[-1] == f"2020년 서울 1인 가구 중 {part}"
    assert res["request_type"] == "change" and res["question"] == talk.asked[-1]


def test_ratio_follow_up_keeps_population_sex_and_survey(talk: Talk) -> None:
    talk.parsed = {**ONE_PERSON, "sex": "여", "asked": {"survey": "인구총조사"}}
    talk.say("2020년 서울 인구총조사 기준 여성 1인 가구 수는?")
    talk.say("그중 70세 이상 비율은?")
    assert talk.extracted == []
    assert talk.asked[-1] == "2020년 서울 인구총조사 기준 여성 1인 가구 중 70세 이상 비율은?"


@pytest.mark.parametrize("age", [65, 85])
def test_ratio_follow_up_keeps_the_previous_age_population(talk: Talk, age: int) -> None:
    talk.parsed = {**ONE_PERSON, "sex": "여", "ages": [[70, None]]}
    talk.say("2020년 서울 70세 이상 여성 1인 가구 수는?")
    talk.parsed = {**ONE_PERSON, "ages": [[age, None]], "cross": {"dynamic": "both"}}
    result = talk.say(f"그중 {age}세 이상 비율은?")
    assert talk.extracted == []
    assert talk.asked[-1] == f"2020년 서울 여성 70세 이상 1인 가구 중 {age}세 이상 비율은?"
    assert result["parsed"]["scoped_age_ratio"] and result["parsed"]["cross"] is None
    assert result["parsed"]["unsupported"]


@pytest.mark.parametrize(
    "question",
    [
        "65세 이상 비율은?",
        "그중 고령인구 비율은?",
        "그중 여성 취업 인구 비율은?",
        "여성 인구 비율은?",
    ],
)
def test_ratio_follow_up_respects_a_named_indicator(question: str) -> None:
    state = {"conditions": {**ONE_PERSON, "ages": [[70, None]]}}
    assert s.ratio_followup(question, state) is None


@pytest.mark.parametrize(
    "question",
    [
        "전체 가구 중 나홀로 가구 비율은?",
        "서울 인구 중 노인 비율은?",
        "그중 여성 가운데 취업자 비율은?",
    ],
)
def test_ratio_follow_up_leaves_questions_that_name_their_own_base(question: str) -> None:
    # 분모를 질문이 말하면 직전 대상 안의 비율이 아니다: 직전 조건을 붙이지 않고 변경 추출에 맡긴다
    conditions = {"type": "value", "indicator": "총인구", "regions": ["서울특별시"], "year": 2025}
    state = {"conditions": conditions}
    assert s.ratio_followup(question, state) is None


def test_ratio_follow_up_after_ratio_is_left_to_change_extraction(talk: Talk) -> None:
    talk.parsed = {**ONE_PERSON, "indicator": RATIO_SPEC["name"], "dynamic_spec": RATIO_SPEC}
    talk.say("2020년 서울 1인 가구 중 70세 이상 비율은?")
    talk.say("그중 여성 비율은?", request_type="change", changes={"sex": "여"})
    assert talk.extracted == ["그중 여성 비율은?"]


@pytest.mark.parametrize("followup", ["부산은?", "부산은 어때?"])
def test_region_only_follow_up_keeps_table_definition(talk: Talk, followup: str) -> None:
    talk.parsed = {**ONE_PERSON, "indicator": RATIO_SPEC["name"], "dynamic_spec": RATIO_SPEC}
    talk.say("2020년 서울 1인 가구 중 70세 이상 비율은?")
    res = talk.say(followup)
    assert talk.extracted == [] and res["request_type"] == "change"
    assert talk.answered[-1]["regions"] == ["부산"]
    assert talk.answered[-1]["dynamic_spec"] == RATIO_SPEC
    talk.say("서울도?")
    assert talk.answered[-1]["regions"] == ["부산", "서울"]


@pytest.mark.parametrize(
    ("question", "place"),
    [("부산은?", "부산의"), ("부산은 어때?", "부산의"), ("서울과 부산은?", "서울 · 부산의")],
)
def test_region_only_first_question_asks_what_to_see(talk: Talk, question, place) -> None:
    res = talk.say(question)
    assert res["status"] == "unclear" and res["request_type"] == s.UNCLEAR
    assert f"{place} 어떤 통계" in res["answer"] and "예)" in res["answer"]
    assert talk.asked == [] and talk.searched == [] and talk.answered == []
    talk.say("부산 출생아 최근 5년 보여 줘")
    assert talk.asked == ["부산 출생아 최근 5년 보여 줘"]


@pytest.mark.parametrize(
    ("first", "asked"),
    [
        ("부산은?", "부산 2020년 출생아 수는?"),
        ("서울과 부산은?", "서울, 부산 2020년 출생아 수는?"),
    ],
)
def test_answer_to_region_only_question_keeps_the_region(talk: Talk, first, asked) -> None:
    talk.say(first)
    res = talk.say("2020년 출생아 수는?")
    assert talk.asked == [asked] and res["request_type"] == "reply"
    assert res["question"] == asked
    talk.say("2021년 출생아 수는?")
    assert talk.asked[-1] != "부산 2021년 출생아 수는?"


@pytest.mark.parametrize(
    "question",
    [
        "2020년 서울 출생아 수는?",
        "2020년 전국 출생아 수는?",
        "우리나라 2020년 출생아 수는?",
        "우리 나라 2020년 출생아 수는?",
        "2020년 국내 출생아 수는?",
        "2020년 국내출생아 수는?",
        "2020년 국내 인구 몇 명이야?",
        "2020년 국내 합계출산율은?",
        "2020년 국내 전체 출생아 수는?",
    ],
)
def test_answer_to_region_only_question_can_name_another_region(talk: Talk, question) -> None:
    talk.say("부산은?")
    talk.say(question)
    assert talk.asked == [question]


@pytest.mark.parametrize(
    "metric",
    [
        "국내이동자 수",
        "국내 이동자 수",
        "국내 인구이동 수",
        "국내출생 다문화가구 자녀 수",
        "다문화가구 자녀 중 국내에서 태어난 자녀 수",
        "국내에서 태어난 다문화가구 자녀 수",
    ],
)
def test_region_only_reply_keeps_place_when_domestic_is_part_of_metric(talk: Talk, metric) -> None:
    talk.say("부산은?")
    question = f"2020년 {metric}는?"
    res = talk.say(question)
    assert res["request_type"] == "reply"
    assert talk.asked == [f"부산 {question}"]


def test_region_only_question_keeps_waiting_after_small_talk(talk: Talk) -> None:
    talk.say("부산은?")
    talk.say("고마워")
    talk.say("2020년 출생아 수는?")
    assert talk.asked == ["부산 2020년 출생아 수는?"]


def test_region_only_first_question_keeps_one_place_in_examples(talk: Talk) -> None:
    res = talk.say("서울 부산은?")
    assert res["answer"].startswith("서울 부산의 어떤 통계")
    assert "2025년 서울 부산 인구" in res["answer"]


def test_region_only_after_numeric_answer_still_changes_region(talk: Talk) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("서울은?")
    assert res["request_type"] == "change" and talk.answered[-1]["regions"] == ["서울"]


def test_region_only_rule_needs_nothing_but_regions() -> None:
    c = {"type": "value", "regions": ["서울"]}
    assert s.region_only_changes("부산은?", c) == {"regions": {"op": "set", "value": ["부산"]}}
    assert s.region_only_changes("서울도?", c) == {"regions": {"op": "add", "value": ["서울"]}}
    assert s.region_only_changes("부산 출생아 수는?", c) is None
    assert s.region_only_changes("부산은 몇 명이야?", c) is None
    assert s.region_only_changes("부산은?", {"type": "rank", "regions": []}) is None
    both = {"regions": {"op": "set", "value": ["서울", "부산"]}}
    assert s.region_only_changes("서울이랑 부산은?", c) == both


def test_region_only_first_question_with_irang_asks_what_to_see(talk: Talk) -> None:
    res = talk.say("서울이랑 부산은?")
    assert res["request_type"] == s.UNCLEAR and res["answer"].startswith("서울 · 부산의 어떤 통계")
    assert talk.asked == [] and talk.searched == []


def test_people_question_does_not_inherit_other_indicator(talk: Talk) -> None:
    talk.parsed = {**ONE_PERSON, "year": 2040}
    talk.say("2040년 서울 1인 가구는?")
    talk.parsed = {**ONE_PERSON, "type": "compare", "indicator": "총인구", "term": None,
                   "regions": ["노원구", "강남구"], "year": 2025}  # fmt: skip
    change = {"regions": {"op": "set", "value": ["노원구", "강남구"]}}
    res = talk.say("노원구가 강남구보다 사람이 많아 적어?", request_type="change", changes=change)
    assert talk.asked[-1] == "노원구가 강남구보다 사람이 많아 적어?"
    assert res["parsed"]["indicator"] == "총인구"
    assert next(a for a in res["applied"] if a["name"] == "지표")["source"] == "user"


def test_inherited_table_indicator_keeps_definition_and_search_text() -> None:
    prev = {**ONE_PERSON, "indicator": RATIO_SPEC["name"], "term": "1인 가구 중 70세 이상 비율",
            "dynamic_spec": RATIO_SPEC}  # fmt: skip
    parsed = {**ONE_PERSON, "indicator": "총인구", "term": None, "regions": ["부산"], "year": None}
    sources = {"indicator": "user", "regions": "user", "period": "default", "sex": "default"}
    p, src = s.inherit(parsed, sources, prev, USER, "부산은 어때?")
    assert src["indicator"] == "inherited" and p["dynamic_spec"] == RATIO_SPEC
    assert s.inherited_question(p) == "2020년 부산 1인 가구 중 70세 이상 비율"


def test_table_search_uses_inherited_conditions(talk: Talk) -> None:
    res = {
        "status": "unknown_indicator",
        "question": "부산은 어때?",
        "request_type": "new",
        "search_question": "2020년 부산 1인 가구",
        "parsed": dict(ONE_PERSON),
        "trace": {},
    }
    asyncio.run(s._attach_tables(res, None, "부산은 어때?", None))
    assert talk.searched[0][0] == "2020년 부산 1인 가구" and res["question"] == "부산은 어때?"


def test_off_domain_question_is_refused_before_asking_region(talk: Talk, monkeypatch) -> None:
    from app.services import search

    async def no_tables():
        return []

    monkeypatch.setattr(search, "load_tables", no_tables)
    res = talk.say("우리 동네 청년 실업률 알려줘")
    assert res["status"] == "out_of_scope" and not res.get("clarification")
    assert "'청년 인구'는 볼 수 있습니다" in res["answer"] and talk.asked == []


def test_sex_ratio_is_not_reported_as_unsplit_sex() -> None:
    spec = {"kind": "ratio", "fixed": {"sex": "여", "ages": None}}
    parsed = {"sex": "여", "dynamic_spec": spec}
    result = {"status": "ok", "value": 53.19, "sex": "계"}
    assert not s.sex_not_split(parsed, result)
    assert s.sex_not_split({"sex": "여"}, result)


def test_changed_conditions_search_with_rebuilt_question(talk: Talk) -> None:
    talk.parsed = {**ONE_PERSON, "term": "1인 가구 중 70세 이상 비율"}
    talk.say("2020년 서울 1인 가구 중 70세 이상 비율은?")
    talk.store[talk.sid][0]["conditions"]["indicator"] = "반려동물 수"
    talk.searched.clear()
    talk.say("부산은?")
    assert talk.answered[-1]["regions"] == ["부산"]
    assert talk.searched[0][0] == "2020년 부산 1인 가구 중 70세 이상 비율"


@pytest.mark.parametrize("status_override", ["missing_data", None], ids=["missing_data", "lookup"])
def test_follow_up_retries_search_when_saved_definition_cannot_read_region(
    talk, monkeypatch, status_override
):
    stale = {
        "name": "1인 가구",
        "sources": [{"table": "T", "level": "전국", "national_only": True}],
    }
    talk.parsed = {**ONE_PERSON, "regions": ["전국"], "dynamic_spec": stale}
    talk.say("2020년 전국 1인 가구 수는?")

    async def regional_lookup(parsed, trace=None):
        year = parsed["year"]
        _, result = lookup._pick(parsed["dynamic_spec"], "시도", year, year, parsed["sex"])
        if status_override:
            result["status"] = status_override
        return {**await talk._answer(parsed, trace), "status": result["status"], "result": result}

    async def compute(question, parsed, found, trace):
        fresh = {**stale, "sources": [{"table": "T", "level": "시도"}]}
        return talk._result({**parsed, "dynamic_spec": fresh})

    monkeypatch.setattr(qa, "answer", regional_lookup)
    monkeypatch.setattr(dynamic_indicator, "compute_from_handoff", compute)
    talk.searched.clear()
    res = talk.say("부산은?")
    assert talk.searched[0][0] == "2020년 부산 1인 가구"
    assert res["status"] == "ok" and res["request_type"] == s.CHANGE
    assert res["parsed"]["dynamic_spec"]["sources"][0]["level"] == "시도"


@pytest.mark.parametrize("term", ["1인 가구", "남성 노인 1인 가구"])
def test_changed_sex_search_keeps_age_and_uses_new_sex(talk: Talk, term: str) -> None:
    talk.parsed = {
        **ONE_PERSON,
        "regions": ["부산"],
        "term": term,
        "ages": [[65, None]],
        "sex": "남",
    }
    talk.say("2020년 부산 남성 노인 1인 가구 수는?")
    talk.store[talk.sid][0]["conditions"]["indicator"] = "반려동물 수"
    talk.searched.clear()
    talk.say("여자는?", request_type="change", changes={"sex": "여"})
    age = "노인" if "노인" in term else "65세 이상"
    assert talk.searched[0][0] == f"2020년 부산 여성 {age} 1인 가구"


@pytest.mark.parametrize(
    ("term", "conditions", "expected"),
    [
        ("남성 인구", {"sex": "여"}, "여성 인구"),
        ("여성 인구 비율", {"sex": "남"}, "여성 인구 비율"),
        ("고령인구 비율", {"sex": "여"}, "여성 고령인구 비율"),
        ("남구 인구", {"sex": "여"}, "여성 남구 인구"),
        ("인구", {"sex": "남", "ages": [[65, None]]}, "남성 65세 이상 인구"),
        ("노인 인구", {"ages": [[65, None]]}, "노인 인구"),
        ("인구", {"sex": "계"}, "인구"),
    ],
)
def test_with_demographics(term, conditions, expected):
    assert s._with_demographics(term, conditions) == expected


@pytest.mark.parametrize("term", ["남성 1인 가구", "남성1인가구"])
def test_switching_to_all_sexes_removes_stale_filter_from_search(term):
    prev = {**ONE_PERSON, "term": term, "sex": "남"}
    changed, _ = s.apply_changes(prev, {"sex": "계"})
    assert "남성" not in s.inherited_question(changed)
    changed, _ = s.apply_changes(prev, {"sex": "여"})
    assert "남성" not in s.inherited_question(changed)
    assert "여성" in s.inherited_question(changed)


def test_same_number_matches_value_unit_and_year_only() -> None:
    # '사전 근거 없음' 규칙이 표 경로로 다시 계산했는데 같은 숫자면 사전 답을 둔다 (11/24 평가)
    a = {"status": "ok", "value": 12802, "unit": "건", "year": 2025}
    assert s.same_number(a, {**a, "indicator": "다른 이름"})
    assert not s.same_number(a, {**a, "value": 12803})
    assert not s.same_number(a, {**a, "unit": "명"})
    assert not s.same_number(a, {**a, "year": 2024})
    assert not s.same_number(a, {"status": "ok", "values": [1, 2]})  # 값 하나가 아니다
    assert not s.same_number(a, None)


@pytest.mark.parametrize(
    ("question", "term"),
    [
        ("20~24세로 보여줘", "20~24세"),
        ("스무 살부터 스물네 살까지로 보여줘", "20~24세"),
        ("스무 살부터 스물네 살까지로 보여줘", "스무 살부터 스물네 살까지"),
        ("스무 살부터 스물네 살까지로 보여줘", "스무 살부터 스물네 살까지로"),
    ],
)
def test_age_only_follow_up_keeps_the_indicator(question, term) -> None:
    """ "서울 청년 미혼 비율" 뒤 "20~24세로 보여줘": HCX가 '20~24세'를 지표로 적어도 연령만 바꾸고
    미혼 비율을 이어받는다 (11/24 S-45, 전에는 연앙인구 표로 564,693.5명이라 답했다)"""
    youth = {"expr": "청년", "range": "19~34세", "basis": "청년기본법 19~34세"}
    prev = {
        **BASE,
        "type": "value",
        "indicator": "미혼 비율",
        "term": "청년",
        "regions": ["서울"],
        "ages": [[19, 34]],
        "age_group": youth,
        "year": None,
        "start_year": None,
        "end_year": None,
        "grounded": True,
    }
    parsed = {
        "type": "value",
        "indicator": "20대 인구",
        "term": term,
        "regions": [],
        "ages": qa.extract_ages(question),
        "sex": "계",
        "grounded": None,
    }
    assert not s.names_indicator(question, parsed)
    p, src = s.inherit(parsed, s.sources_of(question, parsed), prev, {**USER}, question)
    assert p["indicator"] == "미혼 비율" and src["indicator"] == "inherited"
    assert p["ages"] == [[20, 24]] and "age_group" not in p and p["term"] == "미혼 비율"
    assert p["regions"] == ["서울"] and p["grounded"] is True
    rebuilt = s.inherited_question(p)
    assert "20~24세" in rebuilt and "청년" not in rebuilt


def test_term_with_age_and_other_words_still_names_an_indicator() -> None:
    # 연령에 다른 말이 붙은 지표 표현("30대 미혼 인구")은 지금처럼 지표를 말한 것이다
    assert s.names_indicator("30대 미혼 인구는?", {"term": "30대 미혼 인구"})


def test_recent_years_follow_up_is_counted_by_code() -> None:
    """이어 묻는 "최근 5년간 어떻게 변했어?"도 새 질문 경로처럼 자료 마지막 해를 포함한 N개 해
    (11/24 S-44, 전에는 HCX 변경 추출의 기본 기간 6개 해로 답했다)"""
    assert s.recent_years("최근 5년간 어떻게 변했어?") == 5
    assert s.recent_years("10년 동안은?") == 10
    assert s.recent_years("2020년부터 5년간") is None  # 숫자 연도를 말했다
    assert s.recent_years("작년은?") is None  # 한 해는 follow_year가 맡는다
    before = {**BASE, "type": "value", "asked": {"survey": "인구총조사"}}
    c, changed = s.with_recent(before, set(), 5)
    assert c["type"] == "trend" and c["asked"] == {"survey": "인구총조사", "recent": 5}
    assert c["start_year"] is None and c["end_year"] is None and changed == {"period"}
    assert before["asked"] == {"survey": "인구총조사"}  # 앞 조건은 그대로


def test_recent_years_follow_up_sends_recent_to_the_calculation(talk: Talk) -> None:
    talk.say("부산 출생아 2024년 알려줘")
    talk.fail_when = lambda p: bool((p.get("asked") or {}).get("recent"))
    talk.say(
        "최근 5년간 어떻게 변했어?",
        request_type="change",
        changes={"type": "trend", "start_year": 2019, "end_year": 2024},
    )
    asked = talk.answered[0]  # 첫 질문은 해석(qa.ask)으로 답하므로 조건으로 계산한 첫 번째가 이 턴
    assert asked["type"] == "trend" and asked["asked"]["recent"] == 5
    assert asked["start_year"] is None and asked["end_year"] is None  # HCX가 적은 기간은 버린다


def test_parse_restate_reads_relation_and_question() -> None:
    raw = (
        '{"relation": "Followup", "question": " 2024년  대구 출생아 수는? ", "why": "지역만 바꿈"}'
    )
    assert s.parse_restate(raw) == {
        "relation": "followup",
        "question": "2024년 대구 출생아 수는?",
        "why": "지역만 바꿈",
    }
    assert s.parse_restate("말로 답함")["relation"] == ""
    assert s.parse_restate('{"relation": "maybe", "question": "x"}')["relation"] == ""


def test_verify_restate_falls_back_and_checks_indicator_and_period() -> None:
    cond = {"type": "value", "indicator": "출생아 수", "regions": ["부산광역시"], "year": 2024}
    # 못 읽음 → 새 말 그대로 새 질문
    assert s.verify_restate({"relation": "", "question": ""}, cond, "대구는?") == (
        "new",
        "대구는?",
        "복원 못 읽음 → 새 말 그대로",
    )
    # followup 인데 직전 지표가 없음 → 새 질문
    rel, q, note = s.verify_restate(
        {"relation": "followup", "question": "2024년 대구 혼인 건수는?"}, cond, "대구는?"
    )
    assert rel == "new" and q == "2024년 대구 혼인 건수는?" and "직전 지표" in note
    # followup 에 연도가 빠짐 → 직전 기간을 앞에 붙임
    assert s.verify_restate(
        {"relation": "followup", "question": "대구 출생아 수는?"}, cond, "대구는?"
    ) == ("followup", "2024년 대구 출생아 수는?", "직전 기간 2024년 붙임")
    # 상대 시점은 코드가 센 연도로 (test_verify_restate_replaces_llm_year_with_code_relative_year)
    assert (
        s.verify_restate({"relation": "unclear", "question": ""}, cond, "거기는?")[0] == "unclear"
    )


def test_verify_restate_replaces_llm_year_with_code_relative_year(monkeypatch) -> None:
    # "작년은?"의 연도는 코드가 센다: LLM이 적은 2022년을 버리고 follow_year 의 값으로
    monkeypatch.setattr(s, "follow_year", lambda q, c: 2025)
    cond = {"type": "value", "indicator": "혼인 건수", "regions": ["서울특별시"], "year": 2023}
    assert s.verify_restate(
        {"relation": "followup", "question": "2022년 서울 혼인 건수는?"}, cond, "작년은?"
    ) == ("followup", "2025년 서울 혼인 건수는?", "상대 시점은 코드로 2025년")
    assert s.verify_restate(
        {"relation": "new", "question": "서울 인구는?"}, cond, "작년 인구는?"
    ) == ("new", "2025년 서울 인구는?", "상대 시점은 코드로 2025년")


def test_verify_restate_keeps_recent_years_for_code(monkeypatch) -> None:
    # "최근 5년은?"의 기간은 코드가 센다: LLM이 적은 연도를 지우고 '최근 5년'을 앞에
    monkeypatch.setattr(s, "recent_years", lambda q: 5)
    cond = {"type": "value", "indicator": "고령인구 비율", "regions": ["전국"], "year": 2024}
    rel, q, note = s.verify_restate(
        {"relation": "followup", "question": "2023년부터 2020년까지 전국 고령인구 비율은?"},
        cond,
        "최근 5년은 어떻게 변했어?",
    )
    assert (rel, q, note) == (
        "followup",
        "최근 5년 전국 고령인구 비율은?",
        "상대 기간은 코드로 최근 5년",
    )


def test_restate_outcome_separates_broken_from_unfilled() -> None:
    ok = '{"relation": "new", "question": "2024년 대구 출생아 수는?", "why": "x"}'
    assert s.restate_outcome(s.parse_restate(ok), ok) == "ok"
    # 호출 실패 · JSON 없음 → 기존 경로
    assert s.restate_outcome({"relation": "", "question": "", "why": "호출 실패"}, None) == "failed"
    assert s.restate_outcome(s.parse_restate("말로 답함"), "말로 답함") == "failed"
    odd = '{"relation": "maybe", "question": "2024년 대구 출생아 수는?"}'
    assert s.restate_outcome(s.parse_restate(odd), odd) == "failed"
    # JSON 은 읽혔는데 문장을 못 채움 → 되묻기 (11/25 C25)
    empty = '{"relation": "followup", "question": "", "why": "새로 언급한 대상이 없음"}'
    assert s.restate_outcome(s.parse_restate(empty), empty) == "unclear"
    unclear = '{"relation": "unclear", "question": "", "why": "x"}'
    assert s.restate_outcome(s.parse_restate(unclear), unclear) == "unclear"


@pytest.fixture
def restate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s.get_settings(), "follow_restate", True)


def _has_step(res: dict, stage: str, result: str) -> bool:
    return any(x["stage"] == stage and x["result"] == result for x in res["trace"]["flow"])


def test_follow_restate_call_failure_goes_back_to_existing_path(
    talk: Talk, restate_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """복원 호출이 실패하면 새 말만 새 질문으로 보내지 않고 기존 경로(규칙 + 변경 추출)로 이어 간다
    (11/25 HCX 42901: "2020년은?"이 직전 대화 없이 새 질문으로 갔다).
    "2022년만"은 기존 경로의 연도만 규칙(YEAR_ONLY_REPLY)이 변경 추출보다 먼저 잡는다"""
    tried: list[str] = []

    async def broken(state, question, trace):
        tried.append(question)
        raise RuntimeError("HCX 42901")

    monkeypatch.setattr(s, "restate", broken)
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert tried == ["2022년만"]  # 복원을 한 번 시도했다
    assert _has_step(res, "이어 묻기 판단", "실패")
    assert _has_step(res, "후속 질문", "연도 변경")  # 기존 경로의 연도만 규칙을 탔다
    assert res["request_type"] == "change"
    assert res["parsed"]["year"] == 2022 and res["parsed"]["regions"] == ["부산"]
    assert talk.asked == ["부산 출생아 최근 5년 보여 줘"]  # 새 말만으로 새 질문을 하지 않았다


def test_follow_restate_unreadable_answer_goes_back_to_existing_path(
    talk: Talk, restate_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    tried: list[str] = []

    async def prose(question, system):
        tried.append(question)
        return "죄송하지만 이해하지 못했습니다"

    monkeypatch.setattr(s, "_chat", prose)
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    assert tried == ["2022년만"]  # 복원 LLM 을 한 번 불렀다
    assert _has_step(res, "이어 묻기 판단", "실패")
    assert _has_step(res, "후속 질문", "연도 변경")  # 기존 경로의 연도만 규칙을 탔다
    assert res["parsed"]["year"] == 2022 and res["parsed"]["regions"] == ["부산"]
    assert talk.asked == ["부산 출생아 최근 5년 보여 줘"]


def test_follow_restate_unclear_shows_fixed_sentence_not_llm_note(
    talk: Talk, restate_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """unclear 의 why 는 LLM 판단 메모라 화면에 내지 않는다 (11/25 "더" → "새 말이 불완전함 …")"""

    async def unclear(question, system):
        return '{"relation": "unclear", "question": "", "why": "새 말이 불완전함"}'

    monkeypatch.setattr(s, "_chat", unclear)
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("더")
    assert res["request_type"] == "unclear"
    assert res["answer"].startswith(s.RESTATE_UNCLEAR)
    assert "새 말이 불완전함" not in res["answer"]
    assert talk.extracted == []  # unclear 는 복원의 판단이라 기존 경로로 가지 않는다


def test_follow_restate_unfilled_sentence_asks_back(
    talk: Talk, restate_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON 은 읽혔는데 문장이 비면 기존 경로가 아니라 되묻기 (11/25 C25 "거기는 작년에?")"""

    async def unfilled(question, system):
        return '{"relation": "followup", "question": "", "why": "새로 언급한 대상이 없음"}'

    monkeypatch.setattr(s, "_chat", unfilled)
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("거기는 작년에 어땠어?")
    assert res["request_type"] == "unclear"
    assert res["answer"].startswith(s.RESTATE_UNCLEAR)
    assert talk.extracted == []


def test_follow_restate_failure_keeps_llm_raw_in_flow(
    talk: Talk, restate_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실패해 기존 경로로 가도 LLM 이 낸 원문은 처리 흐름에 남는다 (무엇이 깨졌는지 보려고)"""

    async def prose(question, system):
        return "죄송하지만 이해하지 못했습니다"

    monkeypatch.setattr(s, "_chat", prose)
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("2022년만", request_type="change", changes={"year": 2022})
    steps = [x for x in res["trace"]["flow"] if x["stage"] == "이어 묻기 판단"]
    assert steps and steps[0]["result"] == "실패"
    assert "죄송하지만 이해하지 못했습니다" in steps[0]["detail"]


@pytest.fixture
def sure_on(monkeypatch: pytest.MonkeyPatch) -> list:
    """3-a 켬 + 복원 호출을 기록만 하는 가짜 (불리면 기록이 남는다)"""
    monkeypatch.setattr(s.get_settings(), "follow_restate", True)
    monkeypatch.setattr(s.get_settings(), "follow_sure_rules", True)
    called: list = []

    async def spy(state, question, trace):
        called.append(question)
        return {"relation": "new", "question": question, "why": "spy"}

    monkeypatch.setattr(s, "restate", spy)
    return called


def test_sure_rule_picks_only_trusted_rules_after_an_ok_answer() -> None:
    ok = {"last": {"status": "ok"}, "conditions": dict(BASE)}
    assert asyncio.run(s._sure_rule(ok, "서울은?")) == "지역만"
    assert asyncio.run(s._sure_rule(ok, "구성비로 보여줘")) is None  # 규칙도 틀리던 말 → LLM
    assert asyncio.run(s._sure_rule(ok, "작년은?")) is None  # 규칙이 못 잡는 말 → LLM
    # 거절 · 표 안내 뒤에는 규칙을 쓰지 않는다 (C27: 거절된 조건을 이어받았다)
    refused = {"last": {"status": "partial_match"}, "conditions": dict(BASE)}
    assert asyncio.run(s._sure_rule(refused, "서울은?")) is None


def test_sure_rules_skip_restate_for_region_only(talk: Talk, sure_on: list) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    res = talk.say("서울은?")
    assert sure_on == []  # LLM 복원을 부르지 않았다
    assert talk.answered[-1]["regions"] == ["서울"] and res["request_type"] == "change"
    steps = [x for x in res["trace"]["flow"] if x["stage"] == "이어 묻기 판단"]
    assert steps and steps[0]["result"] == "규칙"


def test_sure_rules_skip_restate_for_ratio_follow_up(talk: Talk, sure_on: list) -> None:
    talk.parsed = dict(ONE_PERSON)
    talk.say("2020년 서울 1인 가구 수는?")
    talk.say("그중 여성 비율은?", request_type="change", changes={"sex": "여"})
    assert sure_on == []
    assert talk.asked[-1] == "2020년 서울 1인 가구 중 여성 비율은?"


def test_sure_rules_leave_other_follow_ups_to_restate(talk: Talk, sure_on: list) -> None:
    talk.say("부산 출생아 최근 5년 보여 줘")
    talk.say("작년은?")
    assert sure_on == ["작년은?"]  # 규칙이 못 잡는 말은 LLM 복원으로 ("2022년만"은 연도만 규칙)


def test_sure_rule_names_develop_follow_up_rules() -> None:
    ok = {"last": {"status": "ok"}, "conditions": dict(BASE)}
    ratio = {"last": {"status": "ok"}, "conditions": {**BASE, "indicator": qa.BIRTH_SEX_RATIO}}
    assert asyncio.run(s._sure_rule(ratio, "그럼 반대로는?")) == "출생성비 반대로"
    assert asyncio.run(s._sure_rule(ratio, "1,000명 기준으로는?")) == "출생성비 기준 인원"
    assert asyncio.run(s._sure_rule(ok, "반대로는?")) is None  # 출생성비가 아니면 규칙 아님
    assert asyncio.run(s._sure_rule(ok, "2022년만")) == "연도만"
    assert asyncio.run(s._sure_rule(ok, "재작년까지 5년간은?")) == "끝 연도 기간"
    refused = {"last": {"status": "partial_match"}, "conditions": dict(ratio["conditions"])}
    assert asyncio.run(s._sure_rule(refused, "반대로는?")) is None  # 직전 답이 ok 일 때만


BIRTH_RATIO_2024 = {**BASE, "type": "value", "indicator": qa.BIRTH_SEX_RATIO, "regions": ["전국"],
                    "year": 2024, "start_year": None, "end_year": None}  # fmt: skip
BIRTHS_2024 = {**BASE, "type": "value", "year": 2024, "start_year": None, "end_year": None}
TREND_2021_2025 = {"type": "trend", "start_year": 2021, "end_year": 2025}


def _follow_up(monkeypatch: pytest.MonkeyPatch, sure: bool, parsed: dict, first: str,
               second: str, delta: dict) -> tuple[dict, dict, list]:  # fmt: skip
    """같은 두 턴을 끔(sure=False) 또는 3-a(복원 + 확실한 규칙)로 돌린다
    → (계산 조건, 답, 복원 호출)"""

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 24)

    monkeypatch.setattr(s, "date", FixedDate)
    monkeypatch.setattr(s.timeref, "date", FixedDate)
    monkeypatch.setattr(s.get_settings(), "follow_restate", sure)
    monkeypatch.setattr(s.get_settings(), "follow_sure_rules", sure)
    called: list = []

    async def spy(state, question, trace):
        called.append(question)
        return {"relation": "new", "question": question, "why": "spy"}

    monkeypatch.setattr(s, "restate", spy)
    talk = Talk(monkeypatch)
    talk.parsed = dict(parsed)
    talk.say(first)
    res = talk.say(second, **delta)
    return talk.answered[-1], res, called


@pytest.mark.parametrize(
    ("parsed", "first", "second", "delta", "rule"),
    [
        (BIRTH_RATIO_2024, "2024년 전국 출생성비는?", "반대로는?", {}, "출생성비 반대로"),
        (BIRTH_RATIO_2024, "2024년 전국 출생성비는?", "1000명 기준으로는?", {},
         "출생성비 기준 인원"),
        (BIRTHS_2024, "2024년 부산 출생아 수", "2022년은?", {}, "연도만"),
        (BIRTHS_2024, "2024년 부산 출생아 수", "재작년까지 5년간은?",
         {"request_type": "change", "changes": TREND_2021_2025}, "끝 연도 기간"),
    ],
)  # fmt: skip
def test_sure_rules_run_develop_follow_up_rules_before_restate(
    monkeypatch: pytest.MonkeyPatch, parsed, first, second, delta, rule
) -> None:
    """3-a: develop 의 후속 질문 규칙이 잡는 말은 LLM 복원을 부르지 않고 끔과 같은 조건으로 계산한다
    (반대로는? → 출생성비를 다시 답함, 1000명 기준 · 재작년까지 5년간 → unclear 되묻기 이던 것)"""
    off, off_res, off_called = _follow_up(monkeypatch, False, parsed, first, second, delta)
    sure, res, called = _follow_up(monkeypatch, True, parsed, first, second, delta)
    assert off_called == [] and called == []  # 복원 LLM 을 부르지 않았다
    assert sure == off  # 끔과 같은 조건으로 계산했다
    assert res["request_type"] == off_res["request_type"] == "change"
    assert _has_step(res, "이어 묻기 판단", "규칙")
    assert any(rule in x["detail"] for x in res["trace"]["flow"] if x["stage"] == "이어 묻기 판단")


def test_sure_rules_follow_up_results_match_expected_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """위 네 말이 3-a 에서 실제로 맞는 조건이 되는지 (끔과 같다는 것만으로는 둘 다 틀릴 수 있다)"""
    reverse, *_ = _follow_up(monkeypatch, True, BIRTH_RATIO_2024, "출생성비", "반대로는?", {})
    assert reverse["indicator"] == qa.REVERSE_BIRTH_SEX_RATIO and reverse["year"] == 2024
    count, *_ = _follow_up(
        monkeypatch, True, BIRTH_RATIO_2024, "출생성비", "1000명 기준으로는?", {}
    )
    assert count["indicator"] == qa.BIRTH_SEX_RATIO and count["birth_ratio_base"] == 1000
    year, *_ = _follow_up(monkeypatch, True, BIRTHS_2024, "출생아", "2022년은?", {})
    assert year["year"] == 2022 and year["regions"] == ["부산"]
    change = {"request_type": "change", "changes": TREND_2021_2025}
    span, *_ = _follow_up(monkeypatch, True, BIRTHS_2024, "출생아", "재작년까지 5년간은?", change)
    assert span["type"] == "trend" and (span["start_year"], span["end_year"]) == (2020, 2024)
    assert span["regions"] == ["부산"]


def test_follow_llm_gemma_uses_gemma_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """FOLLOW_LLM=gemma: 복원 LLM 을 GemmaClient 로 부른다 (HCX _chat 은 부르지 않는다)"""
    monkeypatch.setattr(s.get_settings(), "follow_llm", "gemma")
    sent: list = []

    class Fake:
        def __init__(self, **kw):
            pass

        async def chat(self, model, messages, **options):
            sent.append(messages)
            return type("R", (), {"content": '{"verdict": "ok"}'})()

    async def no_hcx(prompt, system):
        pytest.fail("FOLLOW_LLM=gemma 인데 HCX 를 불렀습니다")

    monkeypatch.setattr(s.search_llm, "GemmaClient", Fake)
    monkeypatch.setattr(s, "_chat", no_hcx)
    assert asyncio.run(s._follow_chat("대구는?", "SYS")) == '{"verdict": "ok"}'
    assert sent[0][0] == {"role": "system", "content": "SYS"}
    assert sent[0][1]["content"] == "대구는?"


@pytest.mark.parametrize("question", ["재작년까지 5년간은?", "2024년까지 최근 5년은?"])
def test_follow_up_keeps_user_specified_end_year(talk: Talk, monkeypatch, question):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 24)

    monkeypatch.setattr(s, "date", FixedDate)
    monkeypatch.setattr(s.timeref, "date", FixedDate)
    talk.parsed = {
        **BASE,
        "type": "value",
        "regions": ["서울"],
        "year": 2025,
        "start_year": None,
        "end_year": None,
    }
    talk.say("서울 출생아 수")
    talk.fail_when = lambda p: bool((p.get("asked") or {}).get("recent"))
    result = talk.say(
        question,
        request_type="change",
        changes={"type": "trend", "start_year": 2021, "end_year": 2025},
    )
    assert result["status"] == "ok"
    assert [p["year"] for p in result["result"]["series"]] == list(range(2020, 2025))
    assert {"name": "기간", "value": "2020~2024년", "source": "user"} in result["applied"]


def test_verify_restate_does_not_repeat_recent_years(monkeypatch) -> None:
    # LLM이 이미 '최근 10년간'을 썼으면 앞에 다시 붙인 '최근 10년'과 겹치지 않게 지운다
    monkeypatch.setattr(s, "recent_years", lambda q: 10)
    cond = {"type": "trend", "indicator": "출생아 수", "regions": ["서울"]}
    rel, q, _ = s.verify_restate(
        {"relation": "followup", "question": "최근 10년간 서울 출생아 수 추이는?"},
        cond,
        "10년으로 늘려줘",
    )
    assert (rel, q) == ("followup", "최근 10년 서울 출생아 수 추이는?")


def test_other_subject_question_puts_previous_region_and_period(monkeypatch) -> None:
    # '혼인도 알려줘': 낱말만 표 검색어로 넣지 않고 직전 지역 · 기간을 붙여 질문 이해에 맡긴다
    monkeypatch.setattr(
        s.indicators,
        "resolve_region",
        lambda name: {"status": "ok" if name == "강남" else "unknown_region"},
    )
    value = {"type": "value", "indicator": "출생아 수", "regions": ["서울"], "year": 2025}
    assert s.other_subject_question("혼인도 알려줘", value) == "2025년 서울 혼인도 알려줘"
    rank = {"type": "rank", "indicator": "총인구", "level": "시군구", "sido": "경기도"}
    rank["year"] = 2025
    assert s.other_subject_question("이혼은?", rank) == "2025년 경기도 이혼은?"
    # 지역 이름 · 지표 이름 · 묻는 말은 붙이지 않는다
    assert s.other_subject_question("강남은?", value) is None
    assert s.other_subject_question("사망자 수도 알려줘", value) is None
    assert s.other_subject_question("몇명이야?", value) is None


def test_inherit_judges_subject_by_what_was_said_alone() -> None:
    # 붙인 질문에서도 사용자가 한 말('혼인도 알려줘')로 보고 앞 지표를 이어받지 않는다
    prev = {"type": "value", "indicator": "출생아 수", "regions": ["서울"], "year": 2025}
    parsed = {"type": "value", "regions": ["서울"], "year": 2025}
    p, _ = s.inherit(parsed, {}, prev, {}, "2025년 서울 혼인도 알려줘", "혼인도 알려줘")
    assert p.get("indicator") != "출생아 수" and p.get("term") == "혼인"


# --- 대화 보고서: 변경 추출 · 이어 묻기 복원의 report 선택지 ---


def test_parse_delta_reads_report() -> None:
    d = s.parse_delta('{"request_type": "report", "changes": {}, "chart": null, "unclear": null}')
    assert d["request_type"] == s.REPORT


def test_restate_report_needs_no_question() -> None:
    raw = '{"relation": "report", "question": "", "why": "지금까지 정리"}'
    r = s.parse_restate(raw)
    assert r["relation"] == s.REPORT
    assert s.restate_outcome(r, raw) == s.REPORT


REPORT_STATE = {
    "conditions": {"type": "value", "indicator": "출생아 수", "regions": ["서울"], "year": 2025},
    "sources": {},
    "last": {"status": "ok"},
}
TURNS = [
    {"id": 11, "question": "2025년 서울 출생아 수", "response": {
        "reportable": True, "figure": {"kind": "value"},
        "summary": {"headline": "2025년 서울 출생아 수는 45,516명입니다."}}},
    {"id": 12, "question": "사망자도", "response": {
        "reportable": True, "figure": {"kind": "value"},
        "answer": "2025년 서울 사망자 수는 52,535명입니다.\n출처 …"}},
]  # fmt: skip


def _report_env(monkeypatch, turns):
    monkeypatch.setattr(s, "_session_turns", lambda sid, uid: _async(turns))
    s._session_now.set("sid-1")


async def _async(value):
    return value


def test_report_turn_without_answers_says_so(monkeypatch) -> None:
    _report_env(monkeypatch, [])
    res, _, _ = asyncio.run(s._report_turn(REPORT_STATE, "지금까지 정리해줘", 7))
    assert res["answer"] == s.REPORT_NO_TURNS and res["request_type"] == s.REPORT


def test_report_turn_logged_out_lists_headlines(monkeypatch) -> None:
    _report_env(monkeypatch, TURNS)
    res, _, _ = asyncio.run(s._report_turn(REPORT_STATE, "지금까지 정리해줘", None))
    assert "- 2025년 서울 출생아 수는 45,516명입니다." in res["answer"]
    assert "- 2025년 서울 사망자 수는 52,535명입니다." in res["answer"]
    assert s.REPORT_LOGIN in res["answer"] and res["result"]["report"] is None


def test_report_turn_logged_in_makes_and_writes_report(monkeypatch) -> None:
    _report_env(monkeypatch, TURNS)
    made = {}

    async def create(user_id, turn_ids=None, session_id=None, **_):
        made.update(user_id=user_id, turn_ids=turn_ids, session_id=session_id)
        return {"id": 5}

    async def generate(user_id, report_id):
        content = {
            "title": "통계 분석 보고서",
            "summary": [{"text": "서울 출생아 수는 45,516명입니다."}],
            "analysis": [{"text": "사망자 수가 출생아 수보다 많습니다."}],
            "limits": [],
            "warnings": [],
        }
        return {"id": report_id, "status": "ready", "content": content}

    monkeypatch.setattr(s.reports, "create", create)
    monkeypatch.setattr(s.reports, "generate", generate)
    res, _, _ = asyncio.run(s._report_turn(REPORT_STATE, "보고서로 만들어줘", 7))
    assert made == {"user_id": 7, "turn_ids": [11, 12], "session_id": "sid-1"}
    assert res["result"]["report"] == {"id": 5, "status": "ready"}
    assert res["answer"].splitlines()[:3] == ["통계 분석 보고서", "", "요약"]
    assert "- 사망자 수가 출생아 수보다 많습니다." in res["answer"]


def test_report_turn_shows_report_error(monkeypatch) -> None:
    _report_env(monkeypatch, TURNS)

    async def create(*a, **k):
        raise s.reports.ReportError("not_reportable", "검증된 수치로 답한 결과만 담을 수 있습니다.")

    monkeypatch.setattr(s.reports, "create", create)
    res, _, _ = asyncio.run(s._report_turn(REPORT_STATE, "보고서로 만들어줘", 7))
    assert res["answer"] == "검증된 수치로 답한 결과만 담을 수 있습니다."


def test_report_choice_from_change_extraction_goes_to_report(monkeypatch) -> None:
    # 끔(변경 추출)에서 LLM이 report 를 고르면 대화 보고서로 간다
    state = {
        **REPORT_STATE,
        "last": {"status": "ok", "answer": "…"},
        "recent": ["2025년 서울 출생아 수"],
    }
    monkeypatch.setattr(s, "_chat", lambda prompt, system: _async('{"request_type": "report"}'))
    called = {}

    async def report_turn(st, q, uid):
        called["q"] = q
        return {"status": "ok", "answer": "보고서", "request_type": s.REPORT}, {}, None

    monkeypatch.setattr(s, "_report_turn", report_turn)
    res, _, _ = asyncio.run(s._turn(state, "앞에서 본 결과들 묶어서 정리해줘", None, 7))
    assert called["q"] == "앞에서 본 결과들 묶어서 정리해줘" and res["request_type"] == s.REPORT


def test_whole_talk_summary_falls_back_to_text_when_report_has_no_turns(monkeypatch) -> None:
    # 로그인 사용자의 '지금까지 정리해줘': 보고서에 담을 답이 없으면 대화 전체 요약 글 (11/27)
    state = {
        **REPORT_STATE,
        "last": {"status": "ok", "answer": "2025년 서울 출생아 수는 1명입니다."},
        "history": ["2025년 서울 출생아 수는 1명입니다.", "2025년 부산 출생아 수는 2명입니다."],
    }

    async def report_turn(st, q, uid):
        return {"status": "ok", "answer": s.REPORT_NO_TURNS, "request_type": s.REPORT}, {}, None

    monkeypatch.setattr(s, "_report_turn", report_turn)
    res, _, _ = asyncio.run(s._turn(state, "지금까지 요약해줘", None, 7))
    assert res["request_type"] == s.SUMMARY and "지금까지 본 내용 2가지" in res["answer"]


def test_parse_requests_keeps_order_and_rejects_bad_items() -> None:
    raw = (
        '{"requests": [{"kind": "question", "text": "서울 출생아 수 구하고"},'
        ' {"kind": "summary", "text": "x"}, {"kind": "summary", "text": ""}]}'
    )
    assert s.parse_requests(raw) == [
        {"kind": "question", "text": "서울 출생아 수 구하고"},
        {"kind": "summary", "text": ""},
    ]
    assert s.parse_requests('{"requests": [{"kind": "chart", "text": "막대로"}]}') is None
    assert s.parse_requests('{"requests": [{"kind": "question", "text": ""}]}') is None
    assert s.parse_requests("모르겠음") is None


def test_talk_summary_words_lets_indicator_and_region_through() -> None:
    assert s.talk_summary_words("지금까지 요약해주고 서울 출생아 수도 구해줘")
    assert s.talk_summary_words("혼인 구하고 지금까지 정리해줘")
    assert not s.talk_summary_words("서울 출생아 수 알려줘")


def _talk_state() -> dict:
    return {
        **REPORT_STATE,
        "last": {"status": "ok", "answer": "2025년 서울 출생아 수는 1명입니다."},
        "history": ["2025년 서울 출생아 수는 1명입니다.", "2025년 부산 출생아 수는 2명입니다."],
    }


def test_talk_split_summary_then_question_answers_both(monkeypatch) -> None:
    """'앞에서 본 거 정리해주고 혼인도 구해줘': 정리 글 + 질문 부분만 다시 계산한 답 (11/27)"""
    asked: list[str] = []

    async def split(question, trace):
        return [{"kind": "summary", "text": ""}, {"kind": "question", "text": "혼인도 구해줘"}]

    real_turn = s._turn

    async def turn(state, question, selections=None, user_id=None):
        if s._talk_split_inner.get():
            asked.append(question)
            return {"status": "ok", "answer": "2025년 서울 혼인 건수는 3건입니다.",
                    "request_type": s.NEW, "trace": {}}, {}, None  # fmt: skip
        return await real_turn(state, question, selections, user_id)

    monkeypatch.setattr(s, "split_talk", split)
    monkeypatch.setattr(s, "_turn", turn)
    res, _, _ = asyncio.run(s._turn(_talk_state(), "앞에서 본 거 정리해주고 혼인도 구해줘"))
    assert asked == ["혼인도 구해줘"]
    assert res["answer"].startswith("지금까지 본 내용 2가지")
    assert res["answer"].endswith("2025년 서울 혼인 건수는 3건입니다.")
    assert res["request_type"] == s.NEW and res["talk_summary"]["asked"] == "혼인도 구해줘"


def test_talk_split_question_first_keeps_spoken_order(monkeypatch) -> None:
    async def split(question, trace):
        return [
            {"kind": "question", "text": "서울 출생아 수 구하고"},
            {"kind": "summary", "text": ""},
        ]

    real_turn = s._turn

    async def turn(state, question, selections=None, user_id=None):
        if s._talk_split_inner.get():
            return {"status": "ok", "answer": "새 답", "request_type": s.NEW, "trace": {}}, {}, None
        return await real_turn(state, question, selections, user_id)

    monkeypatch.setattr(s, "split_talk", split)
    monkeypatch.setattr(s, "_turn", turn)
    res, _, _ = asyncio.run(s._turn(_talk_state(), "서울 출생아 수 구하고 지금까지 본 거 요약해줘"))
    assert res["answer"].startswith("새 답") and "지금까지 본 내용 2가지" in res["answer"]


def test_talk_split_failure_with_indicator_keeps_the_old_path(monkeypatch) -> None:
    """나누기 LLM 이 실패하고 지표 · 지역이 있는 말이면 예전처럼 요약으로 보지 않는다"""
    monkeypatch.setattr(s, "_chat", lambda prompt, system: _async('{"request_type": "new"}'))
    called = {}

    async def new(question, state, **kw):
        called["q"] = question
        return {"status": "ok", "answer": "x", "request_type": s.NEW, "trace": {}}, {}, None

    monkeypatch.setattr(s, "_new", new)
    asyncio.run(s._turn(_talk_state(), "지금까지 요약해주고 서울 출생아 수도 구해줘"))
    assert called.get("q") is not None


def test_talk_split_summary_only_follows_the_old_rule(monkeypatch) -> None:
    """LLM 이 '서울 인구 전체 정리해줘'를 정리로만 읽어도, 지표 · 지역 있는 말은 예전처럼 새 질문"""

    async def split(question, trace):
        return [{"kind": "summary", "text": ""}]

    monkeypatch.setattr(s, "split_talk", split)
    monkeypatch.setattr(s, "_chat", lambda prompt, system: _async('{"request_type": "new"}'))
    called = {}

    async def new(question, state, **kw):
        called["q"] = question
        return {"status": "ok", "answer": "x", "request_type": s.NEW, "trace": {}}, {}, None

    monkeypatch.setattr(s, "_new", new)
    res, _, _ = asyncio.run(s._turn(_talk_state(), "서울 인구 전체 정리해줘"))
    assert called.get("q") is not None and res["request_type"] != s.SUMMARY
