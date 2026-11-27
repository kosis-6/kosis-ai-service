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
    session_id: str | None = Field(
        default=None, description="답을 고른 대화. 보고서에는 한 대화의 답만 담는다"
    )
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
        default_factory=list,
        max_length=MAX_ITEMS,
        description="담을 답들. 모두 한 대화의 답이어야 한다 (다르면 409)",
    )
    session_id: str | None = Field(
        default=None,
        max_length=64,
        description="답을 고른 대화. 보내지 않아도 서버가 답에서 정하고, 보내면 맞는지만 본다",
    )
    generate: bool = Field(
        default=False, description="true면 만들고 바로 문장까지 생성 (대화창 [보고서] 버튼)"
    )
    chart_views: dict[int, Literal["line", "bar", "table"]] = Field(
        default_factory=dict, max_length=MAX_ITEMS, description="선택한 답의 차트 표시 방식"
    )


class ReportUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX)
    purpose: str | None = Field(default=None, max_length=PURPOSE_MAX)
    order: list[int] | None = Field(
        default=None, max_length=MAX_ITEMS, description="담긴 turn_id 전부를 새 순서로"
    )


class DraftAdd(BaseModel):
    turn_id: int
    session_id: str | None = Field(
        default=None,
        max_length=64,
        description="답의 대화. 보내지 않아도 서버가 답에서 정하고, 보내면 맞는지만 본다",
    )
