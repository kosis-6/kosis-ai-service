"""통계표 검색 요청 로그 보기 (실전 2): 틀린 안내가 어느 단계 문제인지 추적한다

POST /search 요청마다 PostgreSQL search_logs에 한 행씩 쌓인다 (app/services/search_log.py).
  slots       슬롯 + 보정 내역      → 질문을 잘못 읽었는지 (주제·대상·연도·지역 수준·성별)
  candidates  후보 상위 10개 점수   → 정답 표가 후보에 없었는지(검색), 있는데 밀렸는지(재순위)
              (키워드·임베딩·조건 판정)
  response    최종 응답             → 후보는 맞는데 패밀리·판 선택이나 상태 판단이 틀렸는지
  timings     단계별 시간           → 느린 단계 (슬롯 추출 HCX / 질문 임베딩 / 검색)

실행 (서버 컨테이너 안, 또는 DATABASE_URL을 맞춘 내 PC)
  docker compose exec api python -m scripts.search_log                    # 요약 + 최근 10건
  docker compose exec api python -m scripts.search_log --status ambiguous # 이 상태만
  docker compose exec api python -m scripts.search_log --last 3 --detail  # 후보 10개·HCX 원문까지
  docker compose exec api python -m scripts.search_log --session S_abc123 # 한 대화의 턴 순서대로
"""

import argparse
import json
import statistics
from collections import Counter
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

COLUMNS = (
    "id, created_at, session_id, question, status, tbl_id, slots, candidates, response,"
    " hcx_raw, timings, elapsed_ms, error"
)


def load(
    url: str, last: int, status: str | None = None, session: str | None = None
) -> list[dict[str, Any]]:
    """최근 last건 (오래된 것부터). 테이블이 아직 없으면 빈 목록"""
    import psycopg
    from psycopg.rows import dict_row

    where, params = [], []
    if status:
        where.append("status = %s")
        params.append(status)
    if session:
        where.append("session_id = %s")
        params.append(session)
    sql = f"SELECT {COLUMNS} FROM search_logs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT %s"
    with psycopg.connect(url, row_factory=dict_row) as conn:
        if conn.execute("SELECT to_regclass('search_logs')").fetchone()["to_regclass"] is None:
            return []
        rows = conn.execute(sql, [*params, last]).fetchall()
    return rows[::-1]


def retrieval(record: dict[str, Any]) -> str:
    """후보에 임베딩 점수가 있으면 hybrid (응답의 debug는 요청할 때만 남으므로 후보로 본다)"""
    candidates = record.get("candidates") or []
    if not candidates:
        return "-"
    return "hybrid" if any(c.get("dense") is not None for c in candidates) else "keyword"


def summarize(records: list[dict[str, Any]]) -> str:
    """상태별 건수·평균 응답 시간, 검색 방식, 단계별 시간 중앙값"""
    counts = Counter(r["status"] or "실패" for r in records)
    lines = [f"요청 {len(records)}건", "", "| status | 건수 | 평균 응답(ms) |", "|---|---|---|"]
    for status, n in counts.most_common():
        times = [
            r["elapsed_ms"]
            for r in records
            if (r["status"] or "실패") == status and r["elapsed_ms"] is not None
        ]
        avg = round(sum(times) / len(times)) if times else "-"
        lines.append(f"| {status} | {n} | {avg} |")
    modes = Counter(retrieval(r) for r in records)
    lines += ["", "검색 방식: " + ", ".join(f"{k} {v}" for k, v in modes.most_common())]
    stages = {}
    for key in ("slots_ms", "embed_ms", "search_ms"):
        values = [(r.get("timings") or {}).get(key) for r in records]
        values = [v for v in values if v is not None]
        if values:
            stages[key] = round(statistics.median(values))
    if stages:
        lines.append("단계별 시간 중앙값(ms): " + ", ".join(f"{k} {v}" for k, v in stages.items()))
    return "\n".join(lines)


def _slots_line(slots: dict[str, Any]) -> str:
    c = slots.get("conditions") or {}
    parts = [f"주제={slots.get('topic')!r}"]
    if slots.get("target"):
        parts.append(f"대상={slots['target']!r}")
    parts += [f"{k}={v}" for k, v in c.items() if v not in (None, [], "")]
    for key in ("projection", "spouse", "exclude"):
        if slots.get(key):
            parts.append(f"{key}={slots[key]}")
    if slots.get("parse_error"):
        parts.append("parse_error")
    return " ".join(parts)


def _candidate_line(rank: int, c: dict[str, Any]) -> str:
    dense = "-" if c.get("dense") is None else f"{c['dense']:.3f}"
    failed = [k for k, ok in (c.get("checks") or {}).items() if not ok]
    mark = f" ✘{','.join(failed)}" if failed else ""
    return (
        f"    {rank:2}. {c['score']:.3f} (키워드 {c.get('keyword', 0):.2f} · 임베딩 {dense})"
        f" {c['table_name']} [{c['tbl_id']}]{mark}"
    )


def show(record: dict[str, Any], detail: bool = False) -> str:
    at = record["created_at"]
    at = at.astimezone(KST).strftime("%m-%d %H:%M:%S") if hasattr(at, "astimezone") else at
    head = (
        f"[{at}] #{record['id']} {record['status'] or '실패'} {record.get('elapsed_ms')}ms"
        f" {retrieval(record)} · 세션 {record.get('session_id')}"
    )
    lines = [head, f"  질문: {record['question']}"]
    if record.get("error"):
        lines.append(f"  오류: {record['error']}")
    slots = record.get("slots") or {}
    if slots:
        lines.append(f"  슬롯: {_slots_line(slots)}")
        for fix in slots.get("fixes") or []:
            change = f"{fix.get('from')!r} → {fix.get('to')!r}"
            lines.append(f"  보정: {fix.get('key')} {change} ({fix.get('reason')})")
    recs = (record.get("response") or {}).get("recommendations") or []
    if recs:
        lines.append(
            f"  안내: {recs[0].get('tbl_nm')} [{recs[0].get('tbl_id')}] {recs[0].get('checks')}"
        )
    candidates = record.get("candidates") or []
    if candidates:
        lines.append("  후보:")
        lines += [_candidate_line(i, c) for i, c in enumerate(candidates[: 10 if detail else 5], 1)]
    if detail:
        lines.append(f"  시간: {record.get('timings')}")
        lines.append(f"  HCX 원문: {json.dumps(record.get('hcx_raw'), ensure_ascii=False)[:500]}")
        answer = (record.get("response") or {}).get("answer") or ""
        lines.append("  답변: " + answer.replace("\n", " / ")[:300])
    return "\n".join(lines)


def main() -> None:
    from pipeline.load_catalog import database_url

    parser = argparse.ArgumentParser(description="통계표 검색 요청 로그 보기")
    parser.add_argument("--last", type=int, default=10, help="최근 몇 건 (기본 10)")
    parser.add_argument("--status", help="이 status만 (ok, clarify, ambiguous, out_of_scope …)")
    parser.add_argument("--session", help="이 세션만 (되묻기 흐름 확인)")
    parser.add_argument("--detail", action="store_true", help="후보 10개·HCX 원문·답변까지")
    args = parser.parse_args()

    records = load(database_url(), args.last, args.status, args.session)
    if not records:
        print("기록 없음 (search_logs가 없거나 조건에 맞는 요청이 없음)")
        return
    print(summarize(records))
    print()
    print("\n\n".join(show(r, args.detail) for r in records))


if __name__ == "__main__":
    main()
