"""'표 찾고도 거절' 원인 집계 (scripts/refusal_reasons.py)"""

import json

from scripts import eval_devset
from scripts import refusal_reasons as rr


def tables_turn(*steps: dict) -> dict:
    return {"status": "tables", "trace": {"dynamic": list(steps)}}


def test_only_tables_answers_are_counted() -> None:
    assert rr.reason_of({"status": "ok", "trace": {"dynamic": [{"skip": "오류"}]}}) is None
    assert rr.reason_of(None) is None


def test_picked_table_reason_wins_over_other_candidates() -> None:
    turn = tables_turn(
        {"tbl_id": "T2", "kind": "other", "skip": "검토가 고른 표를 '모두 충족'으로 봄"},
        {"tbl_id": "T1", "kind": "picked", "skip": "표에 성별 구분이 없음"},
    )
    r = rr.reason_of(turn)
    assert (r["key"], r["tbl_id"], r["label"]) == ("no_sex", "T1", "표에 성별 구분이 없음")


def test_cell_none_is_split_by_what_the_llm_said() -> None:
    def one(**llm):
        entry = {"tbl_id": "T", "kind": "picked", "skip": "질문의 말과 맞는 칸이 없음"}
        if llm:
            entry["cell_llm"] = llm
        return rr.reason_of(tables_turn(entry))["sub"]

    assert one(raw={"fits": "아니오"}) == "llm_no"
    assert one(raw={"fits": "예"}) == "llm_yes"
    assert one(error="HcxError: [gemma_530]") == "llm_error"
    assert one() == "rule"


def test_skip_texts_map_to_categories() -> None:
    cases = {
        "통계표 찾기 조건 불충족 (sex, spouse)": "check",
        "적재 안 됨 (too_big, 대기열에 넣음)": "load",
        "성별에서 칸을 고르지 못함": "cell_dim",
        "질문의 조건 '부부 사이가 안 좋아서'을 표의 칸에 반영할 수 없음": "uncovered",
        "'20~24세'에 맞는 연령 구간 없음": "band",
        "연도를 말하지 않았는데 2000년까지인 표": "old_table",
        "처음 보는 문구": "unknown",
    }
    assert {t: rr.category(t) for t in cases} == cases


def test_no_dynamic_and_failed_status_have_their_own_rows() -> None:
    assert rr.reason_of({"status": "tables", "trace": {}})["key"] == "no_try"
    r = rr.reason_of(tables_turn({"tbl_id": "T", "kind": "picked", "status": "unknown_region"}))
    assert (r["key"], r["skip"]) == ("status", "status unknown_region")


def test_request_log_lines_are_read_too(tmp_path) -> None:
    path = tmp_path / "requests.jsonl"
    step = {"kind": "picked", "skip": "표에 연령 구분이 없음"}
    rows = [{"at": "t1", "status": "tables", "dynamic": [step]}, {"at": "t2", "status": "ok"}]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows))
    by, total, _ = rr.collect(rr.turns(path))
    assert total == 1 and by == {"표에 연령 구분이 없음": ["t1"]}


def test_eval_uses_the_final_turn_and_marks_answer_items(tmp_path) -> None:
    path = tmp_path / "x.raw.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "D-1",
                "first": {"status": "clarify"},
                "final": tables_turn({"kind": "picked", "skip": "표에 성별 구분이 없음"}),
            },
            ensure_ascii=False,
        )
    )
    by, total, _ = rr.collect(rr.turns(path))
    lines = rr.table(by, total, answers={"D-1"}, old={}, old_total=0)
    assert "그중 답해야 하는 문항 1건" in lines[0]
    assert any(line.startswith("| 표에 성별 구분이 없음 | 1 | 0 | D-1* |") for line in lines)


def test_eval_columns_and_summary() -> None:
    last = tables_turn({"tbl_id": "T9", "kind": "picked", "skip": "표에 연령 구분이 없음"})
    cols = eval_devset.refusal_columns(last)
    assert cols == {
        "거절 원인": "표에 연령 구분이 없음",
        "거절 원인 문구": "T9 표에 연령 구분이 없음",
    }
    assert eval_devset.refusal_columns({"status": "ok"}) == {"거절 원인": "", "거절 원인 문구": ""}
    recs = [
        {"문항": "D-1", "기대 행동": "답변", **cols},
        {"문항": "D-2", "기대 행동": "답변 불가", **cols},
        {"문항": "D-3", "기대 행동": "답변", "거절 원인": ""},
    ]
    text = "\n".join(eval_devset.refusal_summary(recs))
    assert "통계표 안내로 끝난 턴: 2건, 그중 답해야 하는 문항 1건 (*)" in text
    assert "| 표에 연령 구분이 없음 | 2 | D-1* D-2 | 표 조건 확인 |" in text
    assert eval_devset.refusal_summary([recs[2]]) == []
