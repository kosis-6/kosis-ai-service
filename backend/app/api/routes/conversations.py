from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from app.api.deps import CurrentUser
from app.schemas.conversations import ConversationDetail, ConversationList, ConversationRename
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


@router.delete("/{session_id}", summary="대화 지우기", status_code=204)
async def delete_conversation(session_id: SessionId, user: CurrentUser) -> Response:
    if not await conversations.delete(user.id, session_id):
        raise NOT_FOUND
    return Response(status_code=204)
