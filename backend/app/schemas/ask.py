from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

Question = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
SessionId = Annotated[str, StringConstraints(max_length=64)]


class AskRequest(BaseModel):
    question: Question = Field(
        examples=["2025년 서울과 부산 중 고령인구 비율이 높은 지역은?"],
    )
    session_id: SessionId | None = Field(
        default=None,
        description="대화를 이어 갈 때 앞 응답의 session_id를 그대로 보낸다. 그러면 '서울도 같이',"
        " '2022년만', '막대그래프로'처럼 바뀐 것만 말해도 앞 조건에 이어서 답한다."
        " 없으면 새 대화로 시작한다",
    )
    selections: dict[str, str] | None = Field(
        default=None,
        description="통계표 선택지 버튼으로 고른 값 (family_id / target / sex). /search와 같다",
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
    values: list[float | None] | None = Field(
        default=None,
        description="꺾은선이 여러 개일 때 series 순서의 값 (이때 value는 null)",
    )


class Chart(BaseModel):
    type: Literal["line", "bar", "parts"] = Field(
        description="line: 연도별 꺾은선 / bar: 가로 막대 (큰 순·순위 순) / parts: 구성비 나뉜 막대"
    )
    title: str
    unit: str = Field(description="값의 단위 (%, 명 등)")
    category: str = Field(description="행이 무엇인지 (연도 / 지역 / 항목)")
    rows: list[ChartRow]
    count_unit: str | None = Field(default=None, description="count의 단위")
    series: list[str] | None = Field(
        default=None, description="꺾은선이 여러 개일 때 선 이름 (지역). 없으면 선 하나"
    )
    note: str | None = Field(default=None, description="차트 아래 알림 (일부만 그린 경우 등)")
    view: Literal["line", "bar", "table"] | None = Field(
        default=None,
        description="처음 보여 줄 모양. '막대그래프로', '표로'라고 했을 때 (없으면 type대로)",
    )
    crowded: bool = Field(
        default=False,
        description="꺾은선이 많아(6개 넘음) 겹쳐 그리지 않는다: 지역별 작은 꺾은선으로 그리고 "
        "막대 보기는 두지 않는다. 서버가 정하고 화면은 이 값을 따른다",
    )


class Applied(BaseModel):
    name: str = Field(description="지표 / 지역 / 기간 / 성별")
    value: str
    source: Literal["user", "inherited", "default"] = Field(
        description="user: 사용자가 말함 / inherited: 앞 질문에서 이어받음 / default: 기본값"
    )


class Summary(BaseModel):
    headline: str = Field(description="핵심 한 문장")
    points: list[str] = Field(default_factory=list, description="덧붙일 사실 (최대 4개)")
    cautions: list[str] = Field(
        default_factory=list, description="함께 알릴 주의 (추계값, 인과 판단 안 함 등. 최대 2개)"
    )


class FigureConditions(BaseModel):
    region: str | None = Field(default=None, description="지역 (비교·순위는 지역 목록이나 범위)")
    period: str | None = Field(default=None, description="기간 ('2020년', '2015년~2025년')")
    sex: str | None = Field(default=None, description="성별 (전체면 null)")
    ages: str | None = Field(default=None, description="연령 ('70세 이상')")
    population: str | None = Field(default=None, description="표의 집계 대상 (아직 채우지 않음)")


class Operand(BaseModel):
    label: str = Field(description="분자·분모 이름")
    value: float | None
    unit: str
    conditions: FigureConditions | None = Field(
        default=None, description="이 값의 조건 (아직 채우지 않음)"
    )
    tbl_id: str | None = None


class FigureRow(BaseModel):
    label: str = Field(description="연도 / 지역 / 항목")
    value: float | None
    rank: int | None = None
    count: float | None = Field(default=None, description="구성비일 때 원래 값")


class Figure(BaseModel):
    label: str = Field(description="무엇의 값인지 ('2020년 서울특별시 1인가구')")
    indicator: str
    value: float | None = Field(
        description="대표 수치. 비교·구성비처럼 값이 여럿이면 null이고 rows를 본다"
    )
    unit: str
    kind: Literal["value", "ratio", "diff", "sum", "series", "compare", "ranking", "parts"]
    conditions: FigureConditions
    is_projection: bool = Field(description="장래추계 값인가")
    formula: str | None = None
    operands: list[Operand] = Field(default_factory=list, description="나눈(뺀·더한) 두 값")
    rows: list[FigureRow] = Field(default_factory=list)
    change: dict[str, Any] | None = Field(default=None, description="추이의 처음→끝 변화")
    tbl_ids: list[str] = Field(default_factory=list, description="값을 가져온 통계표")


class QuestionClarification(BaseModel):
    kind: Literal["local_region", "move_scope", "table_cell", "alternative", "report_choice"]
    prompt: str
    options: list[str] = Field(default_factory=list)


class TalkSummary(BaseModel):
    answer: str
    first: Literal["summary", "question"]
    request_type: str | None = None
    asked: str


class ReportReference(BaseModel):
    id: int
    status: Literal["draft", "ready", "failed"]


class AskResponse(BaseModel):
    clarification: QuestionClarification | None = Field(
        default=None,
        description="지역·이사 집계 범위, 표의 칸(table_cell)을 확인하는 질문과 선택지",
    )
    status: str | None = Field(
        description="ok / tables(수치 없이 통계표만 안내) / chat(인사에 대한 답)"
        " / out_of_scope(제공 범위 밖) / not_statistics(통계 질문이 아님)"
        " / 답변 불가 사유 (out_of_range 등)"
    )
    answer: str = Field(description="답변 문장 + 수치·출처 (화면에 그대로 보여줄 답변)")
    parsed: dict[str, Any] = Field(description="1단계 질문 분석 결과")
    result: dict[str, Any] = Field(description="2~4단계 계산 결과")
    sources: list[Source] = Field(description="답변에 쓴 통계표")
    chart: Chart | None = Field(
        default=None, description="그릴 차트. 값 하나짜리 답이나 답변 불가면 null"
    )
    summary: Summary | None = Field(
        default=None,
        description="답의 요약. 수치로 답하지 못했으면 null."
        " '요약해줘'라고 하면 이 내용을 answer로 준다 ('한 줄로'면 headline만)",
    )
    session_id: str | None = Field(
        default=None, description="대화 id. 다음 요청에 함께 보내면 조건을 이어 간다 (1시간)"
    )
    request_type: str | None = Field(
        default=None,
        description="이번 말을 읽은 방식: new(새 질문) / change(조건 변경) / chart(차트 바꾸기)"
        " / summary(요약) / detail(직전 답을 자세히) / source(직전 답의 출처·계산식)"
        " / chat(인사·감사) / reply(되묻기에 대한 답) / unclear(되물음)",
    )
    applied: list[Applied] = Field(default_factory=list, description="이번 답에 적용한 조건과 출처")
    notice: str | None = Field(
        default=None, description="답 위에 보여 줄 알림 (이어받은 조건, 세션 만료 등)"
    )
    tables: dict[str, Any] | None = Field(
        default=None,
        description="통계표 안내. 수치로 답했으면 답에 쓴 표({status: ok, recommendations}),"
        " 수치로 답할 수 없는 질문이면 통계표 찾기 결과(/search 응답과 같은 모양: status, answer,"
        " recommendations, clarify, notice). status가 'tables'면 수치 없이 표만 안내한 답",
    )
    question: str | None = Field(
        default=None,
        description="실제로 답한 질문 (되묻기 답을 앞 질문과 합친 경우 합친 질문)",
    )
    figure: Figure | None = Field(
        default=None,
        description="답의 대표 수치 (result의 여러 모양을 한 형식으로). 수치로 답하지 못했거나"
        " 여러 지표를 묶은 답이면 null",
    )
    turn_id: int | None = Field(
        default=None, description="이 답의 기록 id (로그인한 사용자만). 보고서에 담을 때 쓴다"
    )
    reportable: bool = Field(default=False, description="보고서에 담을 수 있는 답인가")
    report_offer: dict[str, Any] | None = Field(
        default=None,
        description="보고서 제안 칩 {topic, count}. 같은 지표의 담을 수 있는 답이 세 번째일 때"
        " 대화마다 한 번",
    )
    report: dict[str, Any] | None = Field(
        default=None,
        description="보고서 경로(request_type=report)에서 바뀐 뒤의 보고서"
        " (/reports/{id} 응답과 같은 모양)",
    )
    report_ref: ReportReference | None = None
    talk_summary: TalkSummary | None = None
