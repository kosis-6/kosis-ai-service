"""로그인 유저의 대화 기록: 목록, 다시 열기, 제목 바꾸기, 지우기 (app/services/conversations.py)

다시 연 대화를 이어 가려면 /ask에 그 session_id를 보낸다. 새 창은 session_id 없이 보낸다.
다른 계정의 대화는 목록에 없고, id를 알아도 404다.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from app.api.deps import CurrentUser
from app.schemas.conversations import ConversationDetail, ConversationList, ConversationRename
from app.services import conversations


def no_store(response: Response) -> None:
    """대화 내용(사용자의 질문)을 브라우저나 중간 서버가 저장해 두지 않게 한다"""
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
    """최근에 대화한 것부터"""
    return ConversationList(**await conversations.listing(user.id, limit, offset))


@router.get("/{session_id}", summary="대화 하나 다시 열기")
async def get_conversation(session_id: SessionId, user: CurrentUser) -> ConversationDetail:
    """질문과 그때 보여 준 답을 턴 순서대로. 다시 계산하지 않는다"""
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
    """목록, 턴 기록, 이어 갈 조건을 함께 지운다"""
    if not await conversations.delete(user.id, session_id):
        raise NOT_FOUND
    return Response(status_code=204)
