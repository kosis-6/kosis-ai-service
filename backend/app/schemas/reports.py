from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.services.reports import MAX_ITEMS, PURPOSE_MAX, TITLE_MAX


class ReportItemRead(BaseModel):
    turn_id: int = Field(description="담은 답의 /ask turn_id")
    sort_order: int
    added_at: datetime
    snapshot: dict[str, Any] = Field(
        description="담을 때 복사한 화면 내용 (question · answer · figure · sources · chart ·"
        " summary · notice · applied). 원래 대화가 지워져도 남는다"
    )


class ReportRead(BaseModel):
    id: int
    session_id: str | None = Field(default=None, description="만든 대화 (제안으로 만든 경우)")
    title: str
    purpose: str | None = None
    status: Literal["draft", "ready", "failed"] = Field(
        description="draft: 생성 전이거나 항목이 바뀜 / ready: 생성됨 / failed: 생성 실패"
    )
    content: dict[str, Any] | None = Field(
        default=None, description="생성 결과 (다음 단계). 항목이 바뀌면 비워진다"
    )
    created_at: datetime
    updated_at: datetime
    items: list[ReportItemRead]


class ReportSummary(BaseModel):
    id: int
    title: str
    status: str
    created_at: datetime
    updated_at: datetime
    items: int = Field(description="담은 결과 수")


class ReportList(BaseModel):
    total: int
    reports: list[ReportSummary]


class ReportCreate(BaseModel):
    title: str | None = Field(default=None, max_length=TITLE_MAX)
    purpose: str | None = Field(default=None, max_length=PURPOSE_MAX)
    turn_ids: list[int] = Field(
        default_factory=list, max_length=MAX_ITEMS, description="처음부터 담을 답들"
    )
    session_id: str | None = Field(default=None, max_length=64)


class ReportUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    purpose: str | None = Field(default=None, max_length=PURPOSE_MAX)
    order: list[int] | None = Field(
        default=None, max_length=MAX_ITEMS, description="담긴 turn_id 전부를 새 순서로"
    )


class DraftAdd(BaseModel):
    turn_id: int
    session_id: str | None = Field(default=None, max_length=64)
