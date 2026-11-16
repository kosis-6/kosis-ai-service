"""관리자용 사용자 기록 조회 응답 (app/services/user_logs.py)"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class LogUser(BaseModel):
    """기록을 남긴 사용자. 손님(로그인 안 함)이면 모두 비어 있다"""

    user_id: int | None = Field(default=None, description="손님이거나 지워진 사용자면 null")
    user_email: str | None = None
    user_nickname: str | None = None


class TurnRead(LogUser):
    """/ask 대화의 한 턴"""

    id: int
    created_at: datetime
    session_id: str
    turn_no: int
    message: str = Field(description="사용자가 한 말")
    request_type: str | None = Field(
        default=None,
        description="new / change / chart / summary / detail / source / reply / chat / unclear",
    )
    status: str | None = Field(default=None, description="/ask 응답의 status")
    tbl_id: str | None = Field(default=None, description="답에 쓴 통계표")
    recomputed: bool = Field(description="이 턴에 다시 조회·계산했는지")
    answer: str | None = Field(default=None, description="답의 첫 문단")


class TurnList(BaseModel):
    total: int
    turns: list[TurnRead]


class TurnDetail(TurnRead):
    """대화를 펼쳐 볼 때: 조건이 어떻게 바뀌었는지까지"""

    conditions_before: Any | None = None
    conditions_after: Any | None = None
    sources: Any | None = Field(default=None, description="조건마다 어디서 왔는지")


class SessionRead(LogUser):
    """대화 하나"""

    session_id: str
    started_at: datetime
    last_at: datetime
    turns: int
    first_message: str | None = None
    last_status: str | None = None


class SessionList(BaseModel):
    total: int
    sessions: list[SessionRead]


class SessionDetail(LogUser):
    session_id: str
    turns: list[TurnDetail]


class SearchLogRead(LogUser):
    """통계표 찾기 한 번"""

    id: int
    created_at: datetime
    session_id: str | None = None
    question: str
    status: str | None = None
    tbl_id: str | None = Field(default=None, description="첫 번째로 안내한 통계표")
    elapsed_ms: int | None = None
    error: str | None = None


class SearchLogList(BaseModel):
    total: int
    searches: list[SearchLogRead]


class SearchLogDetail(SearchLogRead):
    slots: Any | None = None
    candidates: Any | None = Field(default=None, description="후보 상위 10개와 점수")
    response: Any | None = None
    hcx_raw: Any | None = None
    timings: Any | None = None


class DayCount(BaseModel):
    day: date = Field(description="한국 시간 기준 날짜")
    turns: int
    sessions: int


class NameCount(BaseModel):
    name: str
    count: int


class LogSummary(BaseModel):
    """최근 며칠의 사용량"""

    days: int
    since: datetime
    turns: int = Field(description="/ask 턴 수")
    sessions: int = Field(description="대화 수")
    users: int = Field(description="로그인한 사용자 수 (손님 제외)")
    guest_turns: int = Field(description="손님이 남긴 턴 수")
    searches: int = Field(description="통계표 찾기 횟수")
    search_errors: int = Field(description="통계표 찾기 중 오류가 난 횟수")
    by_day: list[DayCount]
    by_status: list[NameCount]
    by_request_type: list[NameCount]
