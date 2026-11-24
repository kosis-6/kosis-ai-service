from pathlib import Path, PurePosixPath

from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

NOT_BUILT = (
    "화면이 아직 빌드되지 않았습니다.\n"
    "frontend/ 에서 npm install && npm run build 를 실행하거나, "
    "docker compose up 으로 frontend 서비스를 함께 띄우세요.\n"
)


class SpaFiles(StaticFiles):
    def __init__(self, directory: str) -> None:
        super().__init__(directory=directory, html=True, check_dir=False)
        self.config_checked = True

    async def get_response(self, path: str, scope: Scope) -> Response:
        if not (Path(str(self.directory)) / "index.html").is_file():
            return PlainTextResponse(NOT_BUILT, status_code=503)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as e:
            if e.status_code != 404 or "." in PurePosixPath(path).name:
                raise
            response = await super().get_response("index.html", scope)
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["cache-control"] = "no-cache"
        return response
