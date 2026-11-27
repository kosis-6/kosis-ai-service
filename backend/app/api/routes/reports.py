import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from app.api.deps import CurrentUser
from app.core.config import get_settings
from app.schemas.reports import DraftAdd, ReportCreate, ReportList, ReportRead, ReportUpdate
from app.services import reports
from app.services.reports import ReportError


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/reports", tags=["통합 보고서"], dependencies=[Depends(no_store)])
ReportId = Annotated[int, Path(ge=1)]
TurnId = Annotated[int, Path(ge=1)]
STATUS = {
    reports.NOT_FOUND: 404,
    reports.NOT_REPORTABLE: 409,
    reports.FULL: 409,
    reports.BAD_ORDER: 422,
    reports.EMPTY: 409,
    reports.CHANGED: 409,
    reports.OTHER_TALK: 409,
}


def _http(e: ReportError) -> HTTPException:
    return HTTPException(status_code=STATUS.get(e.code, 400), detail=e.message)


@router.get("", summary="내 보고서 목록")
async def list_reports(
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ReportList:
    return ReportList(**await reports.listing(user.id, limit, offset))


@router.post("", summary="보고서 만들기 (대화창 [보고서]: 고른 답으로)", status_code=201)
async def create_report(body: ReportCreate, user: CurrentUser) -> ReportRead:
    """고른 답(turn_ids)으로 보고서를 만든다. 한 대화의 답만 담을 수 있다.
    generate가 true면 문장 생성까지 한 번에 한다 (생성이 실패해도 만든 보고서는 남는다)"""
    try:
        found = await reports.create(
            user.id, body.title, body.purpose, body.turn_ids, body.session_id, body.chart_views
        )
    except ReportError as e:
        raise _http(e) from e
    if body.generate:
        return await _generated(user.id, found["id"])
    return ReportRead(**found)


@router.post("/draft/items", summary="지금 초안에 담기 ([보고서에 담기] 버튼)")
async def add_to_draft(body: DraftAdd, user: CurrentUser) -> ReportRead:
    """가장 최근 초안에 담는다. 초안이 없으면 만든다. 이미 담긴 답이면 그대로 돌려준다"""
    try:
        found = await reports.add_to_draft(user.id, body.turn_id, body.session_id)
    except ReportError as e:
        raise _http(e) from e
    return ReportRead(**found)


@router.get("/{report_id}", summary="보고서 하나")
async def get_report(report_id: ReportId, user: CurrentUser) -> ReportRead:
    found = await reports.detail(user.id, report_id)
    if found is None:
        raise HTTPException(status_code=404, detail="보고서가 없습니다.")
    return ReportRead(**found)


@router.patch("/{report_id}", summary="제목 · 목적 · 순서 바꾸기")
async def update_report(report_id: ReportId, body: ReportUpdate, user: CurrentUser) -> ReportRead:
    try:
        found = await reports.update(user.id, report_id, body.title, body.purpose, body.order)
    except ReportError as e:
        raise _http(e) from e
    return ReportRead(**found)


@router.put("/{report_id}/items/{turn_id}", summary="결과 담기")
async def add_item(report_id: ReportId, turn_id: TurnId, user: CurrentUser) -> ReportRead:
    try:
        found = await reports.add_item(user.id, report_id, turn_id)
    except ReportError as e:
        raise _http(e) from e
    return ReportRead(**found)


@router.delete("/{report_id}/items/{turn_id}", summary="결과 빼기")
async def remove_item(report_id: ReportId, turn_id: TurnId, user: CurrentUser) -> ReportRead:
    try:
        found = await reports.remove_item(user.id, report_id, turn_id)
    except ReportError as e:
        raise _http(e) from e
    return ReportRead(**found)


@router.post("/{report_id}/generate", summary="보고서 만들기 (담긴 결과로 문장 생성)")
async def generate_report(report_id: ReportId, user: CurrentUser) -> ReportRead:
    """담긴 결과를 비교 검사하고 LLM이 요약 · 분석 문장을 쓴다. 숫자는 코드가 채운다.
    시간 제한(REPORT_TIMEOUT)을 넘기면 504이고 보고서는 그대로다"""
    return await _generated(user.id, report_id)


async def _generated(user_id: int, report_id: int) -> ReportRead:
    try:
        found = await asyncio.wait_for(
            reports.generate(user_id, report_id), timeout=get_settings().report_timeout
        )
    except TimeoutError as e:
        raise HTTPException(
            status_code=504, detail="보고서를 만드는 데 시간이 너무 걸렸습니다. 다시 시도해 주세요."
        ) from e
    except ReportError as e:
        raise _http(e) from e
    return ReportRead(**found)


@router.delete("/{report_id}", summary="보고서 지우기", status_code=204)
async def delete_report(report_id: ReportId, user: CurrentUser) -> Response:
    if not await reports.delete(user.id, report_id):
        raise HTTPException(status_code=404, detail="보고서가 없습니다.")
    return Response(status_code=204)
