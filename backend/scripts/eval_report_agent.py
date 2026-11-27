"""보고서 에이전트 멀티턴 개발셋 채점 (명세서 7.7)

개발셋 원본은 구글 드라이브 시트다. 시트를 CSV로 내려받아 --csv 로 넘긴다.
임시 사용자를 만들어 대화를 실제 파이프라인(ask_session.handle)으로 재생하고, 끝나면 지운다.

턴마다 본다
  1. 경로: request_type 이 report 인가 (기대 경로 report / ask)
  2. 담긴 항목: 이 턴이 끝난 뒤 최근 보고서의 항목 · 순서
     (T번호 = 같은 대화의 턴, Tn new = n턴에 새로 조회한 것)
     기대가 T번호 목록이 아니면(예: "조회한 결과 전부") 수동 확인
  3. 금지: 도구 이름 · "ask_stat N회 이상" · "add_item(Tn)" · "확인 없이 add_item"
     · "report 경로로 감"을
     기계로 판정한다. 나머지 금지 문구는 수동 확인
  도구 순서(기대 행동)는 참고로만 남긴다

  uv run python -m scripts.eval_report_agent --csv 시트.csv [--only R01,R04] [--llm hcx|gemma]
"""

import argparse
import asyncio
import csv
import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

OUT_DIR = Path("data/eval")
TOOLS = (
    "find_turns",
    "ask_stat",
    "add_item",
    "remove_item",
    "reorder",
    "set_title",
    "check_compare",
    "write_report",
    "revise_text",
    "decline_offer",
)
ITEMS = re.compile(r"^(T\d+(new)?)(,T\d+(new)?)*$")


RETRY_WAITS = (30, 60, 120)  # HCX 호출 한도(429)에 걸리면 기다렸다 다시


async def ask(sid: str | None, text_: str, user_id: int) -> dict:
    from app.services import ask_session
    from app.services.hcx_client import HcxError

    for wait in (*RETRY_WAITS, None):
        try:
            return await ask_session.handle(sid, text_, user_id)
        except HcxError as e:
            if wait is None or not e.is_rate_limit:
                raise
            print(f"  HCX 호출 한도 → {wait}초 뒤 다시")
            await asyncio.sleep(wait)
    raise RuntimeError("unreachable")


def conversations(rows: list[dict]) -> list[list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["대화 ID"]].append(r)
    return [sorted(t, key=lambda r: int(r["턴"] or 0)) for t in by.values()]


def steps_of(res: dict) -> list[dict]:
    return ((res.get("trace") or {}).get("report_agent") or {}).get("steps") or []


def forbidden(
    rule: str, steps: list[dict], path: str, labels: list[str], before: list[str]
) -> str | None:
    """위반이면 이유, 아니면 None, 기계로 못 보면 '수동'"""
    rule = rule.strip()
    if not rule:
        return None
    called = [s["action"] for s in steps if not (s.get("result") or {}).get("error")]
    if rule == "report 경로로 감":
        return "보고서 경로로 감" if path == "report" else None
    if m := re.fullmatch(r"ask_stat (\d+)회 이상", rule):
        n = sum(1 for s in steps if s["action"] == "ask_stat")
        return f"ask_stat {n}회" if n >= int(m[1]) else None
    if m := re.fullmatch(r"add_item\((T\d+)\)", rule):
        return f"{m[1]} 담음" if m[1] in labels and m[1] not in before else None
    if rule == "확인 없이 add_item":
        fresh = [x for x in labels if x.endswith("new") and x not in before]
        first_turn_new = [
            s for s in steps if s["action"] == "add_item" and (s.get("result") or {}).get("added")
        ]
        return (
            "확인 없이 새 결과를 담음"
            if fresh and first_turn_new and not any(s["action"] == "ask_user" for s in steps)
            else None
        )
    if rule in TOOLS:
        return f"{rule} 호출" if rule in called else None
    return "수동"


async def latest_items(user_id: int) -> list[int]:
    from app.services import reports

    found = await reports.listing(user_id, limit=1)
    if not found["reports"]:
        return []
    report = await reports.detail(user_id, found["reports"][0]["id"])
    return [i["turn_id"] for i in report["items"]]


async def run_conversation(
    turns: list[dict], user_id: int, interval: float, raw, write_line
) -> list[dict]:
    sid, out = None, []
    names: dict[int, str] = {}  # turn_id → T번호
    before: list[str] = []
    for row in turns:
        n = int(row["턴"])
        try:
            res = await ask(sid, row["질문"], user_id)
        except Exception as e:  # 한도를 넘겨 계속 실패: 이 대화의 남은 턴은 오류로
            for left in turns[turns.index(row) :]:
                line = {
                    "문항": left["문항 ID"],
                    "유형": left["유형"],
                    "판정": "오류",
                    "답": str(e)[:120],
                }
                write_line(line)
                out.append(line)
            print(f"{row['문항 ID']} 오류: {e}")
            return out
        sid = res.get("session_id") or sid
        steps = list(steps_of(res))
        if res.get("turn_id") and res.get("request_type") != "report":
            names[res["turn_id"]] = f"T{n}"
        final = None
        if row["사용자 답"] and (res.get("clarification") or {}).get("kind") == "report_choice":
            await asyncio.sleep(interval)
            final = await ask(sid, row["사용자 답"], user_id)
            steps += steps_of(final)
        path = "report" if res.get("request_type") == "report" else "ask"
        ids = await latest_items(user_id)
        for tid in ids:
            names.setdefault(tid, f"T{n}new")
        labels = [names[t] for t in ids]
        expected = row["기대 담긴 항목"].replace(" ", "")
        if ITEMS.fullmatch(expected):
            items_ok = labels == expected.split(",")
        elif not expected:
            items_ok = labels == before if path == "ask" else None
        else:
            items_ok = None  # 수동
        bans = [forbidden(r, steps, path, labels, before) for r in re.split(r"[;；]", row["금지"])]
        broke = [b for b in bans if b and b != "수동"]
        manual = "수동" in bans or items_ok is None
        correct = path == row["기대 경로"] and items_ok is not False and not broke
        last = final or res
        line = {
            "문항": row["문항 ID"],
            "유형": row["유형"],
            "기대 경로": row["기대 경로"],
            "실제 경로": path,
            "기대 담긴 항목": row["기대 담긴 항목"],
            "실제 담긴 항목": ",".join(labels),
            "금지 위반": " / ".join(broke),
            "행동": " > ".join(str(s.get("action")) for s in steps),
            "답": (last.get("answer") or "").split("\n")[0][:120],
            "판정": "오답" if not correct else "수동 확인" if manual else "정답",
        }
        raw.write(
            json.dumps(
                {"id": row["문항 ID"], "first": res, "final": final},
                ensure_ascii=False,
                default=str,
            )
            + "\n"
        )
        raw.flush()
        print(
            f"{line['문항']} {line['판정']} 경로 {path}/{row['기대 경로']}"
            f" 항목 [{line['실제 담긴 항목']}] {line['금지 위반']}"
        )
        write_line(line)
        out.append(line)
        before = labels
        await asyncio.sleep(interval)
    return out


async def make_user() -> int:
    from app.core.db import engine

    async with engine.begin() as conn:
        return (
            await conn.execute(
                text("insert into app.users (email, password_hash) values (:e, 'x') returning id"),
                {"e": f"report-eval-{uuid.uuid4().hex[:10]}@example.com"},
            )
        ).scalar_one()


async def drop_user(user_id: int) -> None:
    from app.core.db import engine

    async with engine.begin() as conn:
        await conn.execute(text("delete from app.chat_turns where user_id = :u"), {"u": user_id})
        await conn.execute(text("delete from app.users where id = :u"), {"u": user_id})


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", required=True, help="드라이브 개발셋 시트를 내려받은 CSV")
    p.add_argument("--only", default="", help="대화 ID 쉼표 목록 (예: R01,R04)")
    p.add_argument(
        "--llm", choices=["hcx", "gemma"], default=None, help="보고서 LLM (기본: 설정값)"
    )
    p.add_argument("--interval", type=float, default=2.0, help="요청 사이 쉬는 초 (호출 한도)")
    args = p.parse_args()

    from app.core.config import get_settings

    if args.llm:
        get_settings().report_llm = args.llm
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8-sig")))
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    convs = [c for c in conversations(rows) if not only or c[0]["대화 ID"] in only]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    llm = get_settings().report_llm
    raw_path = OUT_DIR / f"report_agent_{llm}_{stamp}.jsonl"
    out = OUT_DIR / f"report_agent_{llm}_{stamp}.csv"
    fields = [
        "문항",
        "유형",
        "기대 경로",
        "실제 경로",
        "기대 담긴 항목",
        "실제 담긴 항목",
        "금지 위반",
        "행동",
        "답",
        "판정",
    ]
    lines: list[dict] = []
    # 문항마다 바로 쓴다: 호출 한도 등으로 중간에 멈춰도 그때까지의 결과가 남는다
    with (
        open(raw_path, "w", encoding="utf-8") as raw,
        open(out, "w", newline="", encoding="utf-8-sig") as f,
    ):
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()

        def write_line(line: dict) -> None:
            w.writerow({k: line.get(k, "") for k in fields})
            f.flush()

        for turns in convs:
            user_id = await make_user()  # 대화마다 새 사용자: 보고서가 섞이지 않게
            try:
                lines += await run_conversation(turns, user_id, args.interval, raw, write_line)
            finally:
                await drop_user(user_id)
    count = Counter(x["판정"] for x in lines)
    path_ok = sum(x.get("실제 경로") == x.get("기대 경로") for x in lines)
    print(
        f"\n{llm}: {len(lines)}턴 · 정답 {count['정답']} · 수동 확인 {count['수동 확인']}"
        f" · 오답 {count['오답']} · 오류 {count['오류']}"
    )
    print(
        f"경로 {path_ok}/{len(lines)} · 금지 위반 {sum(bool(x.get('금지 위반')) for x in lines)}턴"
    )
    print(f"결과: {out}\n원본: {raw_path}")


if __name__ == "__main__":
    asyncio.run(main())
