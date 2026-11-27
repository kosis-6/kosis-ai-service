from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from app.api.deps import CurrentUser
from app.schemas.conversations import (
    ChartViewUpdate,
    ConversationDetail,
    ConversationList,
    ConversationRename,
)
from app.services import conversations


def no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(prefix="/conversations", tags=["대화 기록"], dependencies=[Depends(no_store)])
SessionId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
NOT_FOUND = HTTPException(status_code=404, detail="대화가 없습니다.")


@router.get("", summary="내 대화 목록")
async def list_conversations(
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ConversationList:
    return ConversationList(**await conversations.listing(user.id, limit, offset))


@router.get("/{session_id}", summary="대화 하나 다시 열기")
async def get_conversation(session_id: SessionId, user: CurrentUser) -> ConversationDetail:
    found = await conversations.detail(user.id, session_id)
    if found is None:
        raise NOT_FOUND
    return ConversationDetail(**found)


@router.patch("/{session_id}", summary="대화 제목 바꾸기", status_code=204)
async def rename_conversation(
    session_id: SessionId, body: ConversationRename, user: CurrentUser
) -> Response:
    if not await conversations.rename(user.id, session_id, body.title):
        raise NOT_FOUND
    return Response(status_code=204)


@router.put(
    "/{session_id}/turns/{turn_id}/chart-view", summary="답의 차트 모양 남기기", status_code=204
)
async def set_chart_view(
    session_id: SessionId,
    turn_id: Annotated[int, Path(ge=1)],
    body: ChartViewUpdate,
    user: CurrentUser,
) -> Response:
    """화면에서 꺾은선 · 막대 · 표를 바꾸면 그 답에 남긴다 (보고서 · 다시 연 대화가 쓴다)"""
    done = await conversations.set_chart_view(user.id, session_id, turn_id, body.view)
    if done == "missing":
        raise HTTPException(status_code=404, detail="차트가 있는 답이 없습니다.")
    if done == "unfit":
        raise HTTPException(status_code=409, detail="이 차트는 그 모양으로 그릴 수 없습니다.")
    return Response(status_code=204)


@router.delete("/{session_id}", summary="대화 지우기", status_code=204)
async def delete_conversation(session_id: SessionId, user: CurrentUser) -> Response:
    if not await conversations.delete(user.id, session_id):
        raise NOT_FOUND
    return Response(status_code=204)
