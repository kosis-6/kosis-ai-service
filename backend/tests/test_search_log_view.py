from datetime import UTC, datetime

from scripts import search_log as sl


def _record(**extra) -> dict:
    row = {
        "id": 1,
        "created_at": datetime(2026, 10, 31, 10, 38, 51, tzinfo=UTC),
        "session_id": "S_1",
        "question": "돌아가신 분 수",
        "status": "ok",
        "tbl_id": "A",
        "slots": {
            "topic": "사망자 수",
            "target": None,
            "conditions": {"year": None, "sex": None, "classes": []},
            "fixes": [{"key": "sex", "from": "여", "to": None, "reason": "성별 언급 없음"}],
        },
        "candidates": [
            {
                "tbl_id": "A",
                "table_name": "시군구/월별 사망자수",
                "score": 0.75,
                "keyword": 1.0,
                "dense": 0.526,
                "checks": {},
            },
            {
                "tbl_id": "B",
                "table_name": "시도별 인구",
                "score": 0.40,
                "keyword": 0.0,
                "dense": 0.41,
                "checks": {"year": False},
            },
        ],  # fmt: skip
        "response": {"recommendations": [{"tbl_id": "A", "tbl_nm": "시군구/월별 사망자수"}]},
        "hcx_raw": ["{}"],
        "timings": {"slots_ms": 1500, "embed_ms": 160, "search_ms": 16},
        "elapsed_ms": 1520,
        "error": None,
    }
    row.update(extra)
    return row


def test_retrieval_from_candidates() -> None:
    assert sl.retrieval(_record()) == "hybrid"
    keyword_only = [{**c, "dense": None} for c in _record()["candidates"]]
    assert sl.retrieval(_record(candidates=keyword_only)) == "keyword"
    assert sl.retrieval(_record(candidates=[])) == "-"  # 되묻기 답처럼 검색을 다시 하지 않은 턴


def test_summarize_counts_status_mode_and_stage_times() -> None:
    text = sl.summarize([_record(), _record(status="ambiguous", elapsed_ms=1600)])
    assert "요청 2건" in text and "| ok | 1 | 1520 |" in text
    assert "검색 방식: hybrid 2" in text
    assert "embed_ms 160" in text


def test_show_marks_failed_checks_and_uses_korean_time() -> None:
    text = sl.show(_record())
    assert text.startswith("[10-31 19:38:51]")  # UTC 10:38 → 한국 19:38
    assert "보정: sex '여' → None (성별 언급 없음)" in text
    assert "시도별 인구 [B] ✘year" in text
    assert "HCX 원문" not in text and "HCX 원문" in sl.show(_record(), detail=True)
