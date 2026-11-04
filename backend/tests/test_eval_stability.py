from scripts import eval_stability as st


def row(qid: str, action: str, answer: str, hit: bool) -> dict[str, str]:
    return {
        "id": qid,
        "question": "질문",
        "action": action,
        "status": "ok",
        "answer": answer + "\n출처",
        "reason_ok": str(hit),
    }


def test_compare_counts_same_and_different_runs() -> None:
    runs = {
        "s01": [row("s01", "답변", "A", True), row("s01", "답변", "A", True)],
        "s02": [row("s02", "답변", "A", True), row("s02", "범위안내", "B", False)],
    }
    items = {i["id"]: i for i in st.compare(runs)}
    assert items["s01"]["action_same"] and items["s01"]["answer_same"]
    assert not items["s02"]["action_same"] and items["s02"]["expected_hits"] == 1
    report = st.summarize(list(items.values()), [])
    assert "| 매번 같은 행동 | 1 | 50.0% |" in report
