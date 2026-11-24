from contextvars import ContextVar
from typing import Any

_steps: ContextVar[list[dict[str, Any]] | None] = ContextVar("flow_steps", default=None)
MAX_STEPS = 60
DETAIL_MAX = 200


def start() -> list[dict[str, Any]]:
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
    lines = []
    for i, s in enumerate(steps or [], 1):
        detail = f" {s['detail']}" if s.get("detail") else ""
        lines.append(f"{i}. {s['stage']} [{s['result']}]{detail}")
    return "\n".join(lines)


def deciding(steps: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    body = [s for s in steps or [] if s["stage"] != "최종 답"]
    if not body:
        return None
    final = next((s for s in reversed(steps or []) if s["stage"] == "최종 답"), None)
    if final and final["result"] == "ok":
        return next((s for s in reversed(body) if s["result"] == "ok"), body[-1])
    return next((s for s in reversed(body) if s["result"] not in ("ok", "HCX", "캐시")), body[-1])


def summary_of(parsed: dict | None) -> str:
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
