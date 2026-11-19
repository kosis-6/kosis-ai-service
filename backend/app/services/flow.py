"""처리 흐름 기록: 질문 하나가 어떤 단계를 어떤 순서로 거쳐 그 답이 됐는지

질문을 받을 때 start()로 빈 목록을 만들고, 단계마다 add()로 한 줄씩 쌓는다. 목록은 요청마다
따로라(ContextVar) 후보 표를 동시에 계산해도 같은 질문의 목록에 쌓이고, 다른 질문과 섞이지 않는다.
start()를 부르지 않은 곳(단위 테스트, 스크립트)에서는 add()가 아무것도 하지 않는다.

한 줄: {"stage": 단계, "result": 결과, "detail": 덧붙일 말}
  stage  범위 검사 · 질문 이해 · 사전 계산 · 계산 방법 캐시 · 통계표 찾기 · 표 칸으로 계산 ·
         같은 표에서 찾기 · 최종 답
  result ok / 실패 사유(status) / 건너뜀 같은 짧은 말
  detail 그 판단의 근거 (고른 표 · 칸, 못 한 이유)

쓰는 곳: 요청 기록(requests.jsonl의 flow), 개발셋 평가 CSV의 '처리 흐름' · '오답 설명',
         scripts.show_flow (최근 요청의 흐름 보기)
"""

from contextvars import ContextVar
from typing import Any

_steps: ContextVar[list[dict[str, Any]] | None] = ContextVar("flow_steps", default=None)
MAX_STEPS = 60  # 후보 표가 많아도 기록이 너무 길어지지 않게
DETAIL_MAX = 200


def start() -> list[dict[str, Any]]:
    """이 요청의 흐름 목록을 새로 만든다 (/ask 한 턴의 처음)"""
    steps: list[dict[str, Any]] = []
    _steps.set(steps)
    return steps


def current() -> list[dict[str, Any]] | None:
    return _steps.get()


def add(stage: str, result: Any, detail: str = "") -> None:
    steps = _steps.get()
    if steps is None or len(steps) >= MAX_STEPS:
        return
    detail = " ".join(str(detail or "").split())
    if len(detail) > DETAIL_MAX:
        detail = detail[: DETAIL_MAX - 1] + "…"
    steps.append({"stage": stage, "result": str(result or "-"), "detail": detail})


def text(steps: list[dict[str, Any]] | None) -> str:
    """사람이 읽는 여러 줄 ("1. 질문 이해 [HCX] 지표=출생아 수 …")"""
    lines = []
    for i, s in enumerate(steps or [], 1):
        detail = f" {s['detail']}" if s.get("detail") else ""
        lines.append(f"{i}. {s['stage']} [{s['result']}]{detail}")
    return "\n".join(lines)


def deciding(steps: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """답을 정한 단계: 최종 답이 ok면 마지막으로 성공한 계산 단계, 아니면 마지막으로 실패한 단계"""
    body = [s for s in steps or [] if s["stage"] != "최종 답"]
    if not body:
        return None
    final = next((s for s in reversed(steps or []) if s["stage"] == "최종 답"), None)
    if final and final["result"] == "ok":
        return next((s for s in reversed(body) if s["result"] == "ok"), body[-1])
    return next((s for s in reversed(body) if s["result"] not in ("ok", "HCX", "캐시")), body[-1])


def summary_of(parsed: dict | None) -> str:
    """질문 이해 결과 한 줄 (지표 · 유형 · 지역 · 연도 · 성별)"""
    p = parsed or {}
    parts = [
        f"지표={p.get('indicator') or p.get('term') or '-'}",
        f"유형={p.get('type') or '-'}",
        f"지역={','.join(p.get('regions') or []) or '-'}",
    ]
    if p.get("start_year") or p.get("end_year"):
        parts.append(f"기간={p.get('start_year')}~{p.get('end_year')}")
    else:
        parts.append(f"연도={p.get('year') or '-'}")
    if p.get("sex") and p["sex"] != "계":
        parts.append(f"성별={p['sex']}")
    if p.get("cross"):
        c = p["cross"]
        parts.append(f"두 값={c.get('num')} {c.get('form') or '÷'} {c.get('den')}")
    if p.get("grounded") is False:
        parts.append("사전 근거 없음")
    return " ".join(parts)
