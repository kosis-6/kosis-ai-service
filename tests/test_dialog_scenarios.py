"""되묻기 최소 시나리오: 질문부터 표 확정까지 여러 턴을 이어서 본다 (HCX·임베딩·DB 없이).

dialog.handle을 그대로 돌리고, 세션 저장만 메모리로 바꾼다. 상태는 DB처럼 JSON을 거쳐 저장한다.
"""

import asyncio
import json

import pytest

from app.services import dialog as dl
from app.services import search as se
from app.services import slots as sl
from tests.test_search import _naturalized, _t

TARGET_Q = "한국국적을 취득한 자 통계표"


@pytest.fixture
def chat(monkeypatch):
    tables = se.prepare(
        [
            *_naturalized(),
            _t("W1", "외국인 근로자 현황", 2015, 2024, "FW", survey_name="외국인조사"),
            _t("S1", "외국인 유학생 현황", 2015, 2024, "FS", survey_name="외국인조사"),
            _t("M1", "시도별 혼인", 2015, 2024, "FM", survey_name="혼인조사"),
        ]
    )
    sessions: dict[str, str] = {}
    searched: list[str] = []

    async def load_state(session_id):
        saved = sessions.get(session_id)
        return json.loads(saved) if saved else None

    async def save_state(session_id, result, turns):
        waiting = result["status"] == "clarify"
        sessions[session_id] = json.dumps(
            {
                "status": result["status"],
                "family_id": (result.get("family") or {}).get("family_id"),
                "slots": result["slots"],
                "asked": result.get("clarify_axes") if waiting else result.get("options"),
                "clarify_turns": turns,
            },
            ensure_ascii=False,
        )

    async def load_tables():
        return tables

    async def no_dense(question):
        return None

    async def search(question, trace=None, allow_clarify=True):
        searched.append(question)
        slots = sl.correct(question, sl.empty_slots(question))
        return se.find(tables, slots, allow_clarify)

    monkeypatch.setattr(dl, "load_state", load_state)
    monkeypatch.setattr(dl, "save_state", save_state)
    monkeypatch.setattr(dl.search, "load_tables", load_tables)
    monkeypatch.setattr(dl.search, "dense_scores", no_dense)
    monkeypatch.setattr(dl.search, "search", search)

    def ask(text, selections=None, session="S"):
        return asyncio.run(dl.handle(text, session, selections))

    ask.searched = searched
    return ask


def _table(result: dict) -> str:
    return result["table"]["tbl_id"]


def test_target_answer_confirms_table_without_new_search(chat) -> None:
    first = chat(TARGET_Q)
    assert first["status"] == "clarify"
    assert "혼인 귀화자" in first["clarify_axes"][0]["options"]

    done = chat("혼인 귀화자")
    assert done["status"] in ("ok", "partial_match")
    assert done["table"]["tbl_id"].startswith("혼인 귀화자")
    assert chat.searched == [TARGET_Q]  # 답은 다시 검색하지 않는다


def test_answer_can_carry_target_sex_and_year_together(chat) -> None:
    chat(TARGET_Q)
    done = chat("혼인 귀화자, 여성, 2010년")
    assert _table(done) == "혼인 귀화자여_old"  # 2010년을 담은 판 (2007~2015)


def test_anything_falls_back_to_default_member(chat) -> None:
    chat(TARGET_Q)
    done = chat("아무거나")
    assert done["status"] in ("ok", "partial_match")
    assert _table(done).startswith("전체")
    assert done["slots"]["target_default"] is True  # 답변에 '전체로 안내' 고지


def test_screen_button_selection_needs_no_text_match(chat) -> None:
    chat(TARGET_Q)
    done = chat("선택", {"target": "혼인 귀화자", "sex": "여"})
    assert _table(done).startswith("혼인 귀화자여")


def test_clarify_stops_after_two_turns(chat) -> None:
    assert chat(TARGET_Q)["status"] == "clarify"
    # 대상을 말하지 않은 답(성별만)이면 한 번 더 묻는다
    second = chat("여자")
    assert second["status"] == "clarify" and second["turn"] == 1
    # 두 번째 답에서도 대상이 없으면 더 묻지 않고 기본값으로 답한다
    third = chat("2010년")
    assert third["status"] in ("ok", "partial_match") and third["turn"] == 2
    assert _table(third) == "전체여_old"
    assert third["slots"]["target_default"] is True


def test_answer_matching_no_option_is_a_new_question_even_on_last_turn(chat) -> None:
    chat(TARGET_Q)
    assert chat("여자")["status"] == "clarify"  # 이제 성별은 묻는 항목이 아니다
    chat("남자")
    assert chat.searched[1] == "남자"  # 선택지와 안 맞으면 기본값이 아니라 새 질문으로 찾는다


def test_new_question_while_clarifying_starts_over(chat) -> None:
    chat(TARGET_Q)
    done = chat("시도별 혼인")
    assert _table(done) == "M1"
    assert chat.searched == [TARGET_Q, "시도별 혼인"]


def test_finished_session_does_not_treat_next_input_as_answer(chat) -> None:
    chat(TARGET_Q)
    chat("혼인 귀화자")
    assert _table(chat("시도별 혼인")) == "M1"
    assert chat.searched == [TARGET_Q, "시도별 혼인"]


def test_family_choice_by_number_then_member_question(chat) -> None:
    first = chat("외국인 현황")
    assert first["status"] == "ambiguous"
    options = first["options"]
    assert len(options) >= 2

    done = chat("2번")
    assert done["family"]["family_id"] == options[1]["family_id"]
    assert done["status"] in ("ok", "partial_match")
    assert chat.searched == ["외국인 현황"]


def test_sessions_do_not_mix(chat) -> None:
    assert chat(TARGET_Q, session="A")["status"] == "clarify"
    assert _table(chat("시도별 혼인", session="B")) == "M1"
    assert chat("혼인 귀화자", session="A")["table"]["tbl_id"].startswith("혼인 귀화자")
