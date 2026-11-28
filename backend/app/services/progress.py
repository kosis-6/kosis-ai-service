"""답을 기다리는 동안 화면에 보여 줄 처리 단계 (POST /ask 의 progress_id → GET /ask/progress/{id})

flow 는 관리자 화면용 기록이라 단계 이름이 내부 용어다. 여기에는 사용자에게 보일 이름 몇 개만 둔다.
단계는 처리 함수가 시작할 때 now() 로 알린다. 같은 단계로 돌아오면(다시 계산) 맨 뒤로 옮겨서
목록의 마지막이 늘 '지금 하는 일'이다.

uvicorn 한 프로세스의 메모리에 둔다 (세션 잠금 · 캐시와 같은 가정). 답이 끝나면 지우고,
끊긴 요청은 TTL 이 지나면 지운다"""

import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

UNDERSTAND = "질문 이해"
FIND_TABLE = "통계표 찾기"
COMPUTE = "통계 조회·계산"
CHECK = "답 검증"
WRAP_UP = "답변 정리"
REPORT = "보고서 작성"

TTL = 300
MAX_IDS = 1000


class ProgressInUse(Exception):
    """진행 ID가 이미 다른 요청에서 쓰이고 있다."""


@dataclass
class _Request:
    progress_id: str
    expires_at: float
    steps: list[str] = field(default_factory=list)


_store: dict[str, _Request] = {}
_current: ContextVar[_Request | None] = ContextVar("progress_request", default=None)


def begin(progress_id: str | None) -> Token[_Request | None]:
    if progress_id is None:
        return _current.set(None)
    at = time.monotonic()
    for key, entry in list(_store.items()):
        if entry.expires_at <= at:
            del _store[key]
    if progress_id in _store:
        raise ProgressInUse
    if len(_store) >= MAX_IDS:
        del _store[next(iter(_store))]
    entry = _Request(progress_id, at + TTL)
    _store[progress_id] = entry
    return _current.set(entry)


def now(step: str) -> None:
    entry = _current.get()
    if entry is None or _active(entry.progress_id) is not entry:
        return
    steps = entry.steps
    if steps and steps[-1] == step:
        return
    if step in steps:
        steps.remove(step)
    steps.append(step)


def get(progress_id: str) -> list[str]:
    entry = _active(progress_id)
    return list(entry.steps) if entry else []


def end(token: Token[_Request | None]) -> None:
    entry = _current.get()
    if entry is not None and _store.get(entry.progress_id) is entry:
        del _store[entry.progress_id]
    _current.reset(token)


def _active(progress_id: str) -> _Request | None:
    entry = _store.get(progress_id)
    if entry is not None and entry.expires_at <= time.monotonic():
        del _store[progress_id]
        return None
    return entry
