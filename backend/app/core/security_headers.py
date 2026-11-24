from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _csp(**directives: str) -> str:
    return "; ".join(f"{k.replace('_', '-')} {v}" for k, v in directives.items())


COMMON = {
    "object_src": "'none'",
    "base_uri": "'self'",
    "frame_ancestors": "'none'",
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
    worker_src="'self' blob:",
    connect_src="'self'",
    **COMMON,
)
API_CSP = _csp(default_src="'none'", frame_ancestors="'none'")

BASE = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin",
}
HSTS = "max-age=31536000; includeSubDomains"


def headers_for(path: str, scheme: str) -> dict[str, str]:
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
