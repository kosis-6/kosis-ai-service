from fastapi import APIRouter, HTTPException

from app.core.duckdb import DuckDBNotFoundError
from app.schemas.ask import AskRequest, AskResponse
from app.services import qa
from app.services.hcx_client import HcxError

router = APIRouter(tags=["ask"])


@router.post("/ask")
async def ask(body: AskRequest) -> AskResponse:
    try:
        return AskResponse(**await qa.ask(body.question))
    except DuckDBNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except HcxError as e:
        raise HTTPException(status_code=502, detail=f"HCX 호출 실패: {e}") from e
