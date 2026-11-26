from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from app.api.deps import CurrentUser
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


@router.post("", summary="보고서 초안 만들기", status_code=201)
async def create_report(body: ReportCreate, user: CurrentUser) -> ReportRead:
    try:
        found = await reports.create(
            user.id, body.title, body.purpose, body.turn_ids, body.session_id
        )
    except ReportError as e:
        raise _http(e) from e
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


@router.delete("/{report_id}", summary="보고서 지우기", status_code=204)
async def delete_report(report_id: ReportId, user: CurrentUser) -> Response:
    if not await reports.delete(user.id, report_id):
        raise HTTPException(status_code=404, detail="보고서가 없습니다.")
    return Response(status_code=204)
