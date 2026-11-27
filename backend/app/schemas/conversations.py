from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.conversations import TITLE_MAX


class ConversationRead(BaseModel):
    session_id: str = Field(description="/ask에 이 값을 session_id로 보내면 이 대화를 이어 간다")
    title: str = Field(description="첫 질문 (바꿀 수 있다)")
    created_at: datetime
    updated_at: datetime = Field(description="마지막으로 대화한 시각")
    turns: int = Field(description="턴 수")


class ConversationList(BaseModel):
    total: int
    conversations: list[ConversationRead]


class ConversationTurn(BaseModel):
    turn_no: int
    question: str = Field(description="사용자가 한 말")
    created_at: datetime
    response: dict[str, Any] = Field(
        description="그때 보여 준 답 (/ask 응답의 status·answer·chart·tables·notice·applied 등)"
    )


class ConversationDetail(BaseModel):
    session_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    turns: list[ConversationTurn]


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=TITLE_MAX)


class ChartViewUpdate(BaseModel):
    view: Literal["line", "bar", "table"] = Field(description="화면에서 고른 차트 보기")
