"""터미널에서 챗봇에 질문하기.
API POST /search 를 그대로 호출하고, 되묻기는 같은 세션으로 이어 답한다.

준비: docker compose up -d   (처음이면 --build)
실행: python -m scripts.chat            # 대화 모드. 빈 줄 또는 q 로 종료
      (호스트 파이썬 아무거나, 표준 라이브러리만)
      python -m scripts.chat --debug    # 후보 8개·슬롯·007 판정·시간까지 출력
      python -m scripts.chat -q "서울시 1인 가구는 어디에 많이 살아"   # 한 번만
표준 라이브러리만 쓴다.
"""

import argparse
import json
import sys
import urllib.request

BASE = "http://localhost:8000"


def post(question: str, session_id: str | None, debug: bool) -> dict:
    body = json.dumps(
        {"question": question, "session_id": session_id, "debug": debug, "top_k": 3},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/search", data=body, headers={"Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def show(r: dict, debug: bool) -> None:
    print(f"\n[{r.get('status')}]")
    print(r.get("answer") or "(답변 없음)")
    if not debug:
        return
    d = r.get("debug") or {}
    print("\n  슬롯:", json.dumps(r.get("parsed"), ensure_ascii=False))
    if d.get("fixes"):
        print("  보정:", json.dumps(d.get("fixes"), ensure_ascii=False))
    print("  후보 (점수 · 키워드 · 임베딩):")
    for c in (d.get("candidates") or [])[:8]:
        dense = "-" if c["dense"] is None else f"{c['dense']:.3f}"
        print(
            f"    {c['score']:.3f} · {c['keyword']:.2f} · {dense}  "
            f"{c['tbl_id']} {c['table_name']} [{c.get('edition', '')}]"
        )
    lp = d.get("llm_pick") or {}
    if lp:
        print("  007:", json.dumps(lp.get("raw") or lp, ensure_ascii=False)[:300])
    t = d.get("timings") or {}
    if t:
        print("  시간(ms):", " · ".join(f"{k.replace('_ms', '')} {v}" for k, v in t.items()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--question", help="한 번만 묻고 끝낸다")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if not sys.stdin.isatty():  # 파이프로 넣을 때 (터미널 직접 입력은 그대로)
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        urllib.request.urlopen(f"{BASE}/health/search", timeout=10).read()
    except Exception as e:
        print(f"API 에 연결할 수 없습니다 ({e}).")
        print("레포 폴더에서 docker compose up -d 를 먼저 실행하세요.")
        return
    session = None
    if a.question:
        r = post(a.question, None, a.debug)
        show(r, a.debug)
        return
    print("질문을 입력하세요 (빈 줄 또는 q 로 종료).")
    print("되묻기가 오면 번호나 이름으로 이어서 답하면 됩니다.")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q or q.lower() == "q":
            break
        r = post(q, session, a.debug)
        show(r, a.debug)
        # 되묻기·모호면 같은 세션으로 다음 입력을 답으로 보낸다. 끝났으면 새 세션
        session = r.get("session_id") if r.get("status") in ("clarify", "ambiguous") else None
        if session:
            print("  (되묻기 중: 다음 입력은 위 질문에 대한 답으로 보냅니다)")


if __name__ == "__main__":
    main()
