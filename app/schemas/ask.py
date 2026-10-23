from typing import Any

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=500,
        examples=["2025년 서울과 부산 중 고령인구 비율이 높은 지역은?"],
    )


class AskResponse(BaseModel):
    answer: str = Field(description="설명 문장 + 수치·출처 (화면에 그대로 보여줄 답변)")
    parsed: dict[str, Any] = Field(description="1단계 질문 분석 결과")
    result: dict[str, Any] = Field(description="2~4단계 계산 결과 (status로 답변 가능 여부)")
    dropped_numbers: list[str] = Field(
        description="설명 문장에 근거 없는 숫자가 남아 문장을 뺐을 때 그 숫자들"
    )
