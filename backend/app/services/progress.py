"""답을 기다리는 동안 화면에 보여 줄 처리 단계 (POST /ask 의 progress_id → GET /ask/progress/{id})

flow 는 관리자 화면용 기록이라 단계 이름이 내부 용어다. 여기에는 사용자에게 보일 이름 몇 개만 둔다.
단계는 처리 함수가 시작할 때 now() 로 알린다. 같은 단계로 돌아오면(다시 계산) 맨 뒤로 옮겨서
목록의 마지막이 늘 '지금 하는 일'이다.

uvicorn 한 프로세스의 메모리에 둔다 (세션 잠금 · 캐시와 같은 가정). 답이 끝나면 지우고,
끊긴 요청은 TTL 이 지나면 지운다"""

import time
from contextvars import ContextVar

UNDERSTAND = "질문 이해"
FIND_TABLE = "통계표 찾기"
COMPUTE = "통계 조회·계산"
CHECK = "답 검증"
WRAP_UP = "답변 정리"
REPORT = "보고서 작성"

TTL = 300
MAX_IDS = 1000

_store: dict[str, tuple[float, list[str]]] = {}
_current: ContextVar[str | None] = ContextVar("progress_id", default=None)


def begin(progress_id: str | None) -> None:
    """이 요청의 단계를 progress_id 로 모은다. progress_id 가 없으면 아무것도 하지 않는다"""
    if not progress_id:
        return
    _prune()
    _store[progress_id] = (time.monotonic(), [])
    _current.set(progress_id)


def now(step: str) -> None:
    progress_id = _current.get()
    if progress_id is None or (entry := _store.get(progress_id)) is None:
        return
    steps = entry[1]
    if steps and steps[-1] == step:
        return
    if step in steps:
        steps.remove(step)
    steps.append(step)


def get(progress_id: str) -> list[str]:
    entry = _store.get(progress_id)
    return list(entry[1]) if entry else []


def end(progress_id: str | None) -> None:
    if progress_id:
        _store.pop(progress_id, None)


def _prune() -> None:
    old = time.monotonic() - TTL
    for key in [k for k, (at, _) in _store.items() if at < old]:
        _store.pop(key, None)
    while len(_store) >= MAX_IDS:
        _store.pop(next(iter(_store)))
