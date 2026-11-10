"""보안 헤더: 모든 응답에 XSS·클릭재킹·MIME 추측을 막는 헤더를 붙인다

경로마다 Content-Security-Policy(CSP)를 다르게 둔다.
  /chat  (화면)       우리 주소의 스크립트만. 글꼴만 Google Fonts 허용. 인라인 스크립트 금지
  /docs, /redoc       Swagger·ReDoc이 CDN(jsdelivr) 스크립트와 인라인 스크립트를 써서 그만큼 허용
  나머지(API, JSON)   아무것도 불러오지 않는다 (default-src 'none')
화면에 외부 스크립트·이미지·인라인 <script>를 새로 넣으면 여기 CHAT_CSP도 함께 고친다.

/auth 응답은 토큰이 들어 있어 브라우저·프록시가 저장하지 않게 한다(Cache-Control: no-store).
HSTS(https만 쓰기)는 https로 들어온 요청에만 붙인다 (로컬 http에서는 붙이지 않는다).
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _csp(**directives: str) -> str:
    return "; ".join(f"{k.replace('_', '-')} {v}" for k, v in directives.items())


COMMON = {
    "object_src": "'none'",
    "base_uri": "'self'",
    "frame_ancestors": "'none'",  # 다른 사이트가 우리 화면을 iframe으로 못 띄운다 (클릭재킹)
}
CHAT_CSP = _csp(
    default_src="'self'",
    script_src="'self'",
    style_src="'self' https://fonts.googleapis.com",
    font_src="https://fonts.gstatic.com",
    img_src="'self' data:",
    connect_src="'self'",
    form_action="'self'",
    **COMMON,
)
DOCS_CSP = _csp(
    default_src="'self'",
    script_src="'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    style_src="'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
    font_src="'self' https://fonts.gstatic.com",
    img_src="'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly",
    worker_src="'self' blob:",  # ReDoc
    connect_src="'self'",
    **COMMON,
)
API_CSP = _csp(default_src="'none'", frame_ancestors="'none'")

BASE = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",  # frame-ancestors를 모르는 옛 브라우저용
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
}
HSTS = "max-age=31536000; includeSubDomains"


def headers_for(path: str, scheme: str) -> dict[str, str]:
    """이 경로·스킴의 응답에 붙일 보안 헤더"""
    if path == "/chat" or path.startswith("/chat/"):
        csp = CHAT_CSP
    elif path.startswith(("/docs", "/redoc")):
        csp = DOCS_CSP
    else:
        csp = API_CSP
    headers = {**BASE, "content-security-policy": csp}
    if path.startswith("/auth"):
        headers["cache-control"] = "no-store"
    if scheme == "https":
        headers["strict-transport-security"] = HSTS
    return headers


class SecurityHeadersMiddleware:
    """응답 헤더에 보안 헤더를 붙인다. 이미 같은 헤더가 있으면 그 값을 둔다"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        extra = headers_for(scope["path"], scope.get("scheme", "http"))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                have = {k.decode().lower() for k, _ in headers}
                headers += [(k.encode(), v.encode()) for k, v in extra.items() if k not in have]
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
