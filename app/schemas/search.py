from typing import Any

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=500,
        examples=["2010년 한국국적 취득자 중 혼인 귀화자 여자 통계표"],
    )
    session_id: str | None = Field(
        default=None,
        description="없으면 새로 만들어 응답에 돌려준다. 되묻기에 답할 때 같은 값을 보낸다",
    )
    top_k: int = Field(default=3, ge=1, le=5, description="추천 표 개수 (1순위 + 다른 패밀리)")
    debug: bool = Field(default=False, description="검색 후보·슬롯 수정 내역·단계별 시간 포함")


class SearchResponse(BaseModel):
    session_id: str
    status: str = Field(
        description="ok / partial_match / ambiguous / out_of_scope / not_statistics / parse_error"
    )
    answer: str = Field(description="화면에 그대로 보여줄 안내 문장")
    parsed: dict[str, Any] = Field(description="질의 분석 결과 (슬롯)")
    clarify: dict[str, Any] | None = Field(
        description="ambiguous면 패밀리 선택지 (level=family, families)"
    )
    recommendations: list[dict[str, Any]] = Field(description="추천 통계표 (1순위가 확정 표)")
    notice: str | None = Field(description="불충족 조건 등 고지")
    debug: dict[str, Any] | None = None
