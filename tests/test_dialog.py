from app.services import dialog as dl
from app.services import search as se
from app.services import slots as sl
from tests.test_search import _naturalized

ASKED = [
    {"axis": "target", "options": ["전체", "기타", "혼인 귀화자"]},
    {"axis": "sex", "options": ["전체", "남", "여"]},
    {"axis": "edition", "options": ["2006~2015", "2007~2015", "2015"]},
]
OPTIONS = [
    {
        "family_id": "F1",
        "family_name": "한국국적을 취득한 자",
        "table_name": "한국국적을 취득한 자(계)",
    },
    {"family_id": "F2", "family_name": "체류외국인", "table_name": "체류외국인 현황"},
]


def test_match_member() -> None:
    assert dl.match_member("혼인 귀화자, 여성, 2010년", ASKED) == {
        "target": "혼인 귀화자",
        "sex": "여",
        "year": 2010,
    }
    assert dl.match_member("2007~2015년판 남자", ASKED) == {"sex": "남", "edition": "2007~2015"}
    # '전체'는 정해지지 않은 대상·성별 모두에
    assert dl.match_member("전체", ASKED) == {"target": "전체", "sex": "전체"}
    assert dl.match_member("아무거나", ASKED) == {"anything": True}
    assert dl.match_member("시군구별 사망자 수", ASKED) is None


def test_pick_family() -> None:
    assert dl.pick_family("2번", OPTIONS) == "F2"
    assert dl.pick_family("① 이걸로", OPTIONS) == "F1"
    assert dl.pick_family("두 번째", OPTIONS) == "F2"
    assert dl.pick_family("체류외국인", OPTIONS) == "F2"
    # 연도의 앞자리는 번호가 아니다
    assert dl.pick_family("2010년", OPTIONS) is None


def _state(status: str, asked: list, turns: int = 0) -> dict:
    slots = sl.empty_slots("한국국적 취득자 통계표")
    slots["topic"] = "한국국적 취득자"
    return {
        "status": status,
        "family_id": "F1",
        "slots": slots,
        "asked": asked,
        "clarify_turns": turns,
    }


def test_follow_up_clarify() -> None:
    tables = se.prepare(_naturalized())
    result = dl.follow_up(tables, _state("clarify", ASKED), "혼인 귀화자, 여, 2010년")
    assert (result["status"], result["table"]["tbl_id"]) == ("ok", "혼인 귀화자여_old")
    # 대상을 안 말했으면 한 번 더 묻는다
    assert dl.follow_up(tables, _state("clarify", ASKED), "여자")["status"] == "clarify"
    # 마지막 턴이면 기본값(전체)으로 답한다
    result = dl.follow_up(tables, _state("clarify", ASKED, turns=1), "여자")
    assert (result["status"], result["table"]["tbl_id"]) == ("ok", "전체여_old")
    assert result["slots"]["target_default"] is True
    # 화면 버튼 선택
    result = dl.follow_up(
        tables,
        _state("clarify", ASKED),
        "선택",
        {"target": "혼인 귀화자", "edition": "혼인 귀화자계_new"},
    )
    assert result["table"]["tbl_id"] == "혼인 귀화자계_new"
    # 선택지와 관계없는 말이면 새 질문
    assert dl.follow_up(tables, _state("clarify", ASKED), "시군구별 사망자 수") is None


def test_follow_up_ambiguous() -> None:
    tables = se.prepare(_naturalized())
    result = dl.follow_up(tables, _state("ambiguous", OPTIONS), "1번")
    assert (result["status"], result["family"]["family_id"]) == ("clarify", "F1")
    result = dl.follow_up(tables, _state("ambiguous", OPTIONS), "아무거나")
    assert result["family"]["family_id"] == "F1"


def test_handle_retries_rejected_answer_with_previous_question(monkeypatch) -> None:
    """되묻기 답('초혼')이 선택지와 안 맞고 답만으로 찾으면 범위 밖일 때,
    앞 질문에 붙여 다시 찾는다"""
    import asyncio

    prev = sl.empty_slots("신혼부부 나이 차이")
    state = {"status": "ambiguous", "slots": prev, "asked": OPTIONS, "family_id": None,
             "clarify_turns": 0}  # fmt: skip
    asked: list[str] = []

    async def fake_search(question, trace=None, allow_clarify=True):
        asked.append(question)
        status = "out_of_scope" if question == "초혼" else "ambiguous"
        return {"status": status, "slots": sl.empty_slots(question)}

    async def load_state(session_id):
        return state

    async def save_state(session_id, result, turns):
        return None

    async def no_dense(question):
        return None

    async def no_tables():
        return []

    monkeypatch.setattr(dl, "load_state", load_state)
    monkeypatch.setattr(dl, "save_state", save_state)
    monkeypatch.setattr(dl.search, "search", fake_search)
    monkeypatch.setattr(dl.search, "dense_scores", no_dense)
    monkeypatch.setattr(dl.search, "load_tables", no_tables)

    trace: dict = {}
    result = asyncio.run(dl.handle("초혼", "S1", trace=trace))
    assert asked == ["초혼", "신혼부부 나이 차이 초혼"]
    assert result["status"] == "ambiguous" and result["turn"] == 1 and trace["follow_up_merged"]

    # 답만으로 찾은 결과가 거절이 아니면 그대로 쓴다 (다시 찾지 않음)
    asked.clear()
    result = asyncio.run(dl.handle("누적 혼인", "S1"))
    assert asked == ["누적 혼인"] and result["turn"] == 0


def test_match_member_by_target_part() -> None:
    asked = [{"axis": "target", "options": ["서울·인천·경기", "부산·울산·경남", "강원"]}]
    assert dl.match_member("부산", asked) == {"target": "부산·울산·경남"}
