from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LogUser(BaseModel):
    user_id: int | None = Field(default=None, description="손님이거나 지워진 사용자면 null")
    user_email: str | None = None
    user_nickname: str | None = None


class TurnRead(LogUser):
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


class FlowStep(BaseModel):
    stage: str = Field(
        description="범위 검사 / 질문 이해 / 사전 계산 / 계산 방법 캐시 / 통계표 찾기 / "
        "표 칸으로 계산 / 같은 표에서 찾기 / 사전에 없는 쪽 계산 / 최종 답"
    )
    result: str = Field(description="ok, 실패 사유(status), HCX · 캐시 같은 짧은 결과")
    detail: str = Field(default="", description="판단 근거 (고른 표 · 칸, 못 한 이유)")


class TurnDetail(TurnRead):
    conditions_before: Any | None = None
    conditions_after: Any | None = None
    sources: Any | None = Field(default=None, description="조건마다 어디서 왔는지")
    flow: list[FlowStep] | None = Field(
        default=None,
        description="이 턴이 거친 단계와 순서. 처리 흐름을 기록하기 전의 턴은 null",
    )


class SessionRead(LogUser):
    session_id: str
    started_at: datetime
    last_at: datetime
    turns: int
    first_message: str | None = None
    last_status: str | None = None


class PersonRead(LogUser):
    sessions: int = Field(description="대화 창 수 (창을 여러 개 열어 따로 물으면 창마다 하나)")
    turns: int = Field(description="질문(턴) 수")
    first_at: datetime
    last_at: datetime


class PersonList(BaseModel):
    total: int
    people: list[PersonRead]


class SessionList(BaseModel):
    total: int
    sessions: list[SessionRead]


class SessionDetail(LogUser):
    session_id: str
    turns: list[TurnDetail]


class SearchLogRead(LogUser):
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


class AuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    admin_id: int | None = Field(default=None, description="지워진 관리자 계정이면 null")
    admin_email: str
    method: str
    path: str
    query: dict[str, str] | None = Field(default=None, description="주소의 거르는 값")


class AuditList(BaseModel):
    total: int
    records: list[AuditRead]
