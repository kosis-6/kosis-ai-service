"""화면(React) 서빙: 빌드 결과물 폴더(frontend/dist)를 /chat 아래에 내준다

- 있는 파일(assets/*.js, favicon.png)은 그대로 준다
- 화면 안의 주소(/chat/login, /chat/signup)는 파일이 없다 → index.html을 준다.
  브라우저에서 React Router가 주소를 보고 화면을 고른다 (새로고침·주소 직접 입력에 필요)
- 파일 이름처럼 생긴 경로(/chat/assets/없는파일.js)는 404 (index.html을 스크립트로 읽지 않게)
- 아직 빌드하지 않았으면 503과 빌드 방법을 알려 준다 (서버는 그대로 뜬다)
"""

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
        # check_dir=False: 빌드 폴더가 없어도 서버(API)는 뜬다
        super().__init__(directory=directory, html=True, check_dir=False)
        # 첫 요청 때의 폴더 검사도 건너뛴다 (없으면 get_response가 503으로 알린다)
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
            # index.html은 매번 새로 확인한다: 다시 빌드하면 스크립트 파일 이름(해시)이 바뀐다
            response.headers["cache-control"] = "no-cache"
        return response
