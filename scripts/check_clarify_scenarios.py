"""되묻기 시나리오 채점: eval/clarification_scenarios.jsonl을 대화 흐름(dialog.handle)에 넣어 본다

시나리오마다 턴을 차례로 보내고, 턴별 상태(clarify·ambiguous·ok …)와 마지막 표를 기대값과 비교한다.
대화 상태는 DB(chat_sessions) 대신 메모리에 두어 실제 세션을 건드리지 않는다
(tests/test_dialog_scenarios.py와 같은 방식, 상태는 DB처럼 JSON을 거쳐 저장).

시나리오 파일 한 줄 = 한 문항
  turns[]          user(보낼 말), expected_status, selections(화면 버튼 값, 선택),
                   session(세션 이름 꼬리, 선택), expected_questions(되물을 축·선택지, 선택),
                   expected_turn·expected_target_default (선택)
  final_table      마지막 턴에 안내해야 할 표 (tbl_id가 null이면 표는 보지 않음)

expected_status 읽는 법
  ok              ok 또는 partial_match
  ok_or_clarify   ok·partial_match·clarify (번호로 패밀리를 고른 뒤 대상을 더 물을 수 있음)
  new_question    앞 턴과 다른 패밀리로 새로 검색했는가
  그 밖           그 상태와 같아야 함 (clarify, ambiguous …)
'N번'으로 답한 턴은 앞 턴 선택지 N번째 패밀리로 확정됐는지도 본다.

결과: eval/results/clarify_scenarios_YYYYMMDD.md
실행
  python -m scripts.check_clarify_scenarios              # 실제 검색 (HCX 슬롯 + 임베딩)
  python -m scripts.check_clarify_scenarios --rule-slots # HCX 없이 규칙 슬롯 (빠름, 대화 규칙 점검용)
"""

import argparse
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.services import dialog
from app.services import search
from app.services import slots as slot_parser

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "eval" / "clarification_scenarios.jsonl"
RESULTS = ROOT / "eval" / "results"
NUMBER = re.compile(r"^\s*(\d+)\s*번?\s*$")
OK = ("ok", "partial_match")


def read_scenarios(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class MemorySessions:
    """dialog.load_state·save_state를 메모리로 바꾼다 (DB 세션과 같은 필드)"""

    def __init__(self) -> None:
        self.saved: dict[str, str] = {}

    async def load_state(self, session_id: str) -> dict[str, Any] | None:
        saved = self.saved.get(session_id)
        return json.loads(saved) if saved else None

    async def save_state(self, session_id: str, result: dict[str, Any], turns: int) -> None:
        waiting = result["status"] == "clarify"
        self.saved[session_id] = json.dumps(
            {
                "status": result["status"],
                "family_id": (result.get("family") or {}).get("family_id"),
                "slots": result["slots"],
                "asked": result.get("clarify_axes") if waiting else result.get("options"),
                "clarify_turns": turns,
            },
            ensure_ascii=False,
        )


def family_id(result: dict[str, Any]) -> str | None:
    return (result.get("family") or {}).get("family_id") or (result.get("table") or {}).get(
        "family_id"
    )


def status_ok(expected: str, result: dict[str, Any], before: dict[str, Any] | None) -> bool:
    got = result["status"]
    if expected == "ok":
        return got in OK
    if expected == "ok_or_clarify":
        return got in (*OK, "clarify")
    if expected == "new_question":
        return before is not None and family_id(result) != family_id(before)
    return got == expected


def options_diff(expected: list[dict[str, Any]] | None, result: dict[str, Any]) -> str | None:
    """되물을 축·선택지가 기대와 다르면 한 줄 설명 (선택지가 문장으로 적힌 축은 보지 않음)"""
    if not expected or result["status"] != "clarify":
        return None
    got = {a["axis"]: a["options"] for a in result.get("clarify_axes") or []}
    for axis in expected:
        want = axis["options"]
        if not isinstance(want, list) or axis["axis"] == "family":
            continue
        if axis["axis"] not in got:
            return f"{axis['axis']} 축을 묻지 않음"
        if set(got[axis["axis"]]) != set(want):
            return f"{axis['axis']} 선택지 다름: {got[axis['axis']]}"
    return None


async def run_one(scenario: dict[str, Any], sessions: MemorySessions) -> dict[str, Any]:
    sessions.saved.clear()
    turns_out = []
    before: dict[str, Any] | None = None
    passed = True
    for i, turn in enumerate(scenario["turns"]):
        if turn.get("user") is None:
            break
        session = scenario["id"] + turn.get("session", "")
        result = await dialog.handle(turn["user"], session, turn.get("selections"))
        problems = []
        if not status_ok(turn["expected_status"], result, before):
            problems.append(f"상태 {result['status']} (기대 {turn['expected_status']})")
        diff = options_diff(turn.get("expected_questions"), result)
        if diff:
            problems.append(diff)
        if "expected_turn" in turn and result.get("turn") != turn["expected_turn"]:
            problems.append(f"되묻기 {result.get('turn')}번째 (기대 {turn['expected_turn']})")
        if turn.get("expected_target_default") and not result["slots"].get("target_default"):
            problems.append("'전체로 안내' 고지 없음")
        number = NUMBER.match(turn["user"])
        if number and before and before.get("options"):
            idx = int(number.group(1)) - 1
            options = before["options"]
            if idx < len(options) and family_id(result) != options[idx].get("family_id"):
                problems.append(f"{idx + 1}번 패밀리로 확정되지 않음")
        passed &= not problems
        turns_out.append(
            {"index": i + 1, "user": turn["user"], "result": result, "problems": problems}
        )
        before = result
    want = scenario["final_table"]["tbl_id"]
    got = (before or {}).get("table") or {}
    if want and got.get("tbl_id") != want:
        passed = False
        turns_out[-1]["problems"].append(f"표 {got.get('tbl_id')} (기대 {want})")
    return {"scenario": scenario, "passed": passed, "turns": turns_out, "table": got}


def shown(result: dict[str, Any]) -> str:
    table = result.get("table") or {}
    if table:
        return f"{table['tbl_id']} {table['table_name']}"
    if result.get("clarify_axes"):
        return " / ".join(
            f"{a['axis']}: {', '.join(map(str, a['options']))}" for a in result["clarify_axes"]
        )
    if result.get("options"):
        options = enumerate(result["options"], 1)
        return " / ".join(f"{n}. {o.get('family_name')}" for n, o in options)
    return result.get("reason") or ""


def cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def report(runs: list[dict[str, Any]], mode: str) -> str:
    total = len(runs)
    passed = sum(r["passed"] for r in runs)
    groups: dict[str, list[bool]] = {}
    for r in runs:
        groups.setdefault(r["scenario"]["group"], []).append(r["passed"])
    lines = [
        f"# 되묻기 시나리오 채점 ({datetime.now():%Y-%m-%d %H:%M})",
        "",
        f"- 시나리오: `{SCENARIOS.relative_to(ROOT)}` {total}문항",
        f"- 검색: {mode}",
        f"- **통과 {passed} / {total}**",
        "",
        "| 묶음 | 통과 |",
        "| --- | --- |",
        *[f"| {g} | {sum(v)} / {len(v)} |" for g, v in groups.items()],
        "",
        "## 문항별",
        "",
        "| 문항 | 결과 | 턴 | 보낸 말 | 상태 | 챗봇이 보여 준 것 | 문제 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in runs:
        for t in r["turns"]:
            first = t["index"] == 1
            lines.append(
                "| {id} | {mark} | {n} | {user} | {status} | {shown} | {prob} |".format(
                    id=r["scenario"]["id"] if first else "",
                    mark=("✅" if r["passed"] else "❌") if first else "",
                    n=t["index"],
                    user=cell(t["user"]),
                    status=t["result"]["status"],
                    shown=cell(shown(t["result"]))[:120],
                    prob=cell("; ".join(t["problems"])),
                )
            )
    return "\n".join(lines) + "\n"


async def main_async(path: Path, rule_slots: bool) -> Path:
    sessions = MemorySessions()
    dialog.load_state = sessions.load_state
    dialog.save_state = sessions.save_state
    if rule_slots:

        async def rule_search(question, trace=None, allow_clarify=True):
            tables = await search.load_tables()
            slots = slot_parser.correct(question, slot_parser.empty_slots(question))
            dense = await search.dense_scores(question)
            return search.find(tables, slots, allow_clarify, dense)

        dialog.search.search = rule_search
    runs = [await run_one(s, sessions) for s in read_scenarios(path)]
    mode = "규칙 슬롯 (HCX 없음)" if rule_slots else "실제 검색 (HCX 슬롯 + 임베딩)"
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"clarify_scenarios_{datetime.now():%Y%m%d}.md"
    out.write_text(report(runs, mode), encoding="utf-8")
    passed = sum(r["passed"] for r in runs)
    print(f"통과 {passed} / {len(runs)} → {out.relative_to(ROOT)}")
    for r in runs:
        if not r["passed"]:
            probs = [p for t in r["turns"] for p in t["problems"]]
            print(f"  ❌ {r['scenario']['id']}: {'; '.join(probs)}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=SCENARIOS)
    parser.add_argument("--rule-slots", action="store_true", help="HCX 없이 규칙 슬롯으로 검색")
    args = parser.parse_args()
    asyncio.run(main_async(args.file, args.rule_slots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
