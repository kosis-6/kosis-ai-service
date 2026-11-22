"""질문 이해 + 조건 추출 합치기 (UNDERSTAND_MERGED=gemma, 실험 2-A)"""

import asyncio
import json
from datetime import date

import pytest

from app.core.config import get_settings
from app.prompts import understand_merged as prompt
from app.services import qa, search_llm, slots
from app.services import understand_merged as um
from app.services.hcx_client import ChatResult, HcxError

SEARCH = {
    "topic": "인구 수",
    "search_query": "성 및 연령별 인구",
    "target": None,
    "exclude": [],
    "classes": [],
    "items": ["60대"],
    "query_type": "find_table",
}


def merged(common: dict, numeric: dict, search: dict | None = None) -> dict:
    return {"common": common, "search": search or SEARCH, "numeric": numeric}


def test_split_puts_common_cells_into_both_shapes() -> None:
    com = {
        "regions": ["대전 서구"],
        "region_level": None,
        "year": 2018,
        "year_end": None,
        "sex": "남",
    }
    num, sea = um.split(merged(com, {"type": "value", "indicator": "총인구", "term": "60대 남자"}))
    assert (num["regions"], num["year"], num["start_year"], num["sex"]) == (
        ["대전 서구"],
        2018,
        None,
        "남",
    )
    # 생략한 기본값 칸은 코드가 채운다 (lean)
    assert (num["level"], num["top"], num["ascending"], num["by"]) == ("시도", None, False, None)
    c = sea["conditions"]
    assert (c["region"], c["year"], c["sex"], c["items"]) == ("대전 서구", 2018, "남", ["60대"])
    assert sea["topic"] == "인구 수"


def test_split_periods() -> None:
    num, sea = um.split(
        merged({"year": 2012, "year_end": 2019}, {"type": "trend", "indicator": "x"})
    )
    assert (num["year"], num["start_year"], num["end_year"]) == (2019, 2012, 2019)
    assert (sea["conditions"]["year"], sea["conditions"]["year_end"]) == (2012, 2019)
    # 추이인데 끝이 빠짐: 한 해로 바꾸지 않는다 (계산이 마지막 해까지)
    num, _ = um.split(merged({"year": 2012}, {"type": "trend", "indicator": "x"}))
    assert (num["year"], num["start_year"], num["end_year"]) == (None, 2012, None)


def test_split_vague_region_and_sex() -> None:
    com = {"regions": ["우리 동네"], "sex": "전체"}
    num, sea = um.split(merged(com, {"type": "value", "indicator": "x"}))
    assert num["regions"] == ["우리 동네"] and sea["conditions"]["region"] is None
    assert num["sex"] == "계" and sea["conditions"]["sex"] == "전체"
    # 지역이 여럿이면 통계표 찾기 쪽은 하나만 담지 못해 비운다
    num, sea = um.split(
        merged({"regions": ["울산", "제주"]}, {"type": "compare", "indicator": "x"})
    )
    assert num["regions"] == ["울산", "제주"] and sea["conditions"]["region"] is None
    assert um.split({"common": {}}) == (None, None)


def test_keep_and_take_are_per_question() -> None:
    um.start()
    um.keep("목포  인구 알려줘", {"topic": "a"})
    assert um.take("목포 인구 알려줘") == {"topic": "a"}  # 띄어쓰기 차이는 같은 질문
    assert um.take("목포 출생아 알려줘") is None
    um.start()  # 새 턴은 비어 있다
    assert um.take("목포 인구 알려줘") is None


def test_extract_uses_kept_slots_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_call(question):
        raise AssertionError("조건 추출 LLM을 부르면 안 된다")

    monkeypatch.setattr(slots, "_chat", no_call)
    um.start()
    um.keep("목포 인구 알려줘", {"topic": "인구"})
    trace: dict = {}
    assert asyncio.run(slots.extract("목포 인구 알려줘", trace)) == {"topic": "인구"}
    assert trace["hcx_raw"]


def test_prompt_has_dates_and_both_rule_sets() -> None:
    text = prompt.system("총인구(명)", date(2026, 11, 21), 2025)
    assert "오늘: 2026-11-21" in text and "자료 마지막 해: 2025" in text
    assert "{LATEST}" not in text and "{INDICATORS}" not in text and "총인구(명)" in text
    assert "작년 = 2025" in text and "year 2015, year_end 2025" in text
    assert "- target:" in text and "- type" not in text and "- indicator:" in text
    # 공통 칸으로 옮긴 규칙은 한 번만 (지역 규칙이 두 번 들어가지 않는다)
    assert text.count("- regions:") == 1 and text.count("- region:") == 0
    assert text.count("- sex:") == 1


@pytest.fixture
def merged_on(monkeypatch: pytest.MonkeyPatch) -> dict:
    """UNDERSTAND_MERGED=gemma. Gemma 응답은 state["reply"] (예외면 실패),
    HCX 호출 수는 state["hcx"]"""
    state: dict = {"reply": None, "hcx": 0}
    monkeypatch.setattr(get_settings(), "understand_merged", "gemma")
    monkeypatch.setattr(get_settings(), "question_cache", False)
    monkeypatch.setattr(search_llm, "is_gemma", lambda: True)
    monkeypatch.setattr(search_llm, "falling_back", lambda: False)
    monkeypatch.setattr(qa.indicators, "_data_years", lambda: (2008, 2025))
    monkeypatch.setattr(qa, "_indicator_list", lambda: "총인구(명)")
    monkeypatch.setattr(qa, "correct", lambda question, parsed: parsed)

    async def gemma(self, model, messages, **options):
        assert options["responseFormat"] == {"type": "json_object"}
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return ChatResult(
            json.dumps(state["reply"], ensure_ascii=False), "stop", {"completionTokens": 9}
        )

    async def hcx(prompt_text, system, temperature=0.1, trace=None):
        state["hcx"] += 1
        return '{"type": "value", "indicator": "총인구", "regions": ["목포"], "year": 2020}'

    monkeypatch.setattr(search_llm.GemmaClient, "chat", gemma)
    monkeypatch.setattr(qa, "_chat", hcx)
    return state


def test_understand_merged_answers_without_hcx(merged_on: dict) -> None:
    com = {"regions": ["목포"], "year": 2020}
    merged_on["reply"] = merged(com, {"type": "value", "indicator": "총인구", "term": "인구"})
    um.start()
    trace: dict = {}
    parsed = asyncio.run(qa.understand("2020년 목포 인구 알려줘", trace))
    assert (
        parsed["indicator"] == "총인구" and parsed["regions"] == ["목포"] and parsed["year"] == 2020
    )
    assert merged_on["hcx"] == 0 and trace["understand_llm"] == "gemma_merged"
    kept = um.take("2020년 목포 인구 알려줘")
    assert kept["conditions"]["region"] == "목포" and kept["conditions"]["year"] == 2020


@pytest.mark.parametrize("reply", [HcxError("gemma_unreachable", "끊김"), {"common": {}}])
def test_understand_merged_falls_back_to_hcx(merged_on: dict, reply) -> None:
    merged_on["reply"] = reply  # Gemma 실패 · 읽을 수 없는 출력 → 지금 방식(HCX)
    um.start()
    parsed = asyncio.run(qa.understand("2020년 목포 인구 알려줘", {}))
    assert merged_on["hcx"] == 1 and parsed["regions"] == ["목포"]
    assert um.take("2020년 목포 인구 알려줘") is None  # 조건 추출은 통계표 찾기가 따로 부른다


def test_understand_off_does_not_call_gemma(
    merged_on: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "understand_merged", "off")
    merged_on["reply"] = AssertionError("off면 Gemma를 부르지 않는다")
    asyncio.run(qa.understand("2020년 목포 인구 알려줘", {}))
    assert merged_on["hcx"] == 1
