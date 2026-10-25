from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=500,
        examples=["2025년 서울과 부산 중 고령인구 비율이 높은 지역은?"],
    )


class Source(BaseModel):
    name: str = Field(description="출처 (작성기관, 통계표 이름과 ID)")
    basis: str = Field(description="조사 기준 (기준 시점 등)")
    tbl_id: str | None = Field(default=None, description="KOSIS 통계표 ID")
    url: str | None = Field(default=None, description="KOSIS 통계표 화면 주소")


class AskResponse(BaseModel):
    status: str | None = Field(description="ok 또는 답변 불가 사유 (out_of_range 등)")
    answer: str = Field(description="답변 문장 + 수치·출처 (화면에 그대로 보여줄 답변)")
    parsed: dict[str, Any] = Field(description="1단계 질문 분석 결과")
    result: dict[str, Any] = Field(description="2~4단계 계산 결과")
    sources: list[Source] = Field(description="답변에 쓴 통계표")
