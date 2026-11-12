from typing import Any, Literal

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(
        min_length=1,
        max_length=500,
        examples=["2025년 서울과 부산 중 고령인구 비율이 높은 지역은?"],
    )
    session_id: str | None = Field(
        default=None,
        description="대화를 이어 갈 때 앞 응답의 session_id를 그대로 보낸다. 그러면 '서울도 같이',"
        " '2022년만', '막대그래프로'처럼 바뀐 것만 말해도 앞 조건에 이어서 답한다."
        " 없으면 새 대화로 시작한다",
    )


class Source(BaseModel):
    name: str = Field(description="출처 (작성기관, 통계표 이름과 ID)")
    basis: str = Field(description="조사 기준 (기준 시점 등)")
    tbl_id: str | None = Field(default=None, description="KOSIS 통계표 ID")
    url: str | None = Field(default=None, description="KOSIS 통계표 화면 주소")


class ChartRow(BaseModel):
    label: str = Field(description="행 이름 (연도, 지역, 항목)")
    value: float | None = Field(description="값. 구성비(parts)는 비중(%). 자료가 없으면 null")
    count: float | None = Field(default=None, description="구성비일 때 그 항목의 원래 값")


class Chart(BaseModel):
    """화면이 그릴 차트 (app/services/charts.py). 값은 result에 있는 숫자 그대로다"""

    type: Literal["line", "bar", "parts"] = Field(
        description="line: 연도별 꺾은선 / bar: 가로 막대 (큰 순·순위 순) / parts: 구성비 나뉜 막대"
    )
    title: str
    unit: str = Field(description="값의 단위 (%, 명 등)")
    category: str = Field(description="행이 무엇인지 (연도 / 지역 / 항목)")
    rows: list[ChartRow]
    count_unit: str | None = Field(default=None, description="count의 단위")
    note: str | None = Field(default=None, description="차트 아래 알림 (일부만 그린 경우 등)")
    view: Literal["line", "bar", "table"] | None = Field(
        default=None,
        description="처음 보여 줄 모양. '막대그래프로', '표로'라고 했을 때 (없으면 type대로)",
    )


class Applied(BaseModel):
    """이번 답에 적용한 조건 하나"""

    name: str = Field(description="지표 / 지역 / 기간 / 성별")
    value: str
    source: Literal["user", "inherited", "default"] = Field(
        description="user: 사용자가 말함 / inherited: 앞 질문에서 이어받음 / default: 기본값"
    )


class AskResponse(BaseModel):
    status: str | None = Field(description="ok 또는 답변 불가 사유 (out_of_range 등)")
    answer: str = Field(description="답변 문장 + 수치·출처 (화면에 그대로 보여줄 답변)")
    parsed: dict[str, Any] = Field(description="1단계 질문 분석 결과")
    result: dict[str, Any] = Field(description="2~4단계 계산 결과")
    sources: list[Source] = Field(description="답변에 쓴 통계표")
    chart: Chart | None = Field(
        default=None, description="그릴 차트. 값 하나짜리 답이나 답변 불가면 null"
    )
    session_id: str | None = Field(
        default=None, description="대화 id. 다음 요청에 함께 보내면 조건을 이어 간다 (1시간)"
    )
    request_type: str | None = Field(
        default=None,
        description="이번 말을 읽은 방식: new(새 질문) / change(조건 변경) / chart(차트 바꾸기)"
        " / summary(요약) / reply(되묻기에 대한 답) / unclear(되물음)",
    )
    applied: list[Applied] = Field(default_factory=list, description="이번 답에 적용한 조건과 출처")
    notice: str | None = Field(
        default=None, description="답 위에 보여 줄 알림 (이어받은 조건, 세션 만료 등)"
    )
    question: str | None = Field(
        default=None,
        description="실제로 답한 질문 (되묻기 답을 앞 질문과 합친 경우 합친 질문)",
    )
