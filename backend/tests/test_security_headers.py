"""보안 헤더: 경로마다 맞는 CSP, 공통 헤더, /auth 캐시 금지, https에서만 HSTS"""

from fastapi.testclient import TestClient

from app.core.security_headers import CHAT_CSP, DOCS_CSP, headers_for
from app.main import app

client = TestClient(app)


def test_api_response_has_security_headers() -> None:
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert r.headers["content-security-policy"].startswith("default-src 'none'")
    assert "strict-transport-security" not in r.headers  # http에서는 붙이지 않는다
    assert "cache-control" not in r.headers


def test_chat_page_csp_allows_only_own_scripts() -> None:
    r = client.get("/chat/")  # 화면을 빌드하지 않은 환경(503)에서도 헤더는 같다
    csp = r.headers["content-security-policy"]
    assert csp == CHAT_CSP
    assert "script-src 'self';" in csp  # 인라인·외부 스크립트 금지
    assert "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp
    assert client.get("/chat/assets/index.js").headers["content-security-policy"] == CHAT_CSP


def test_docs_csp_allows_swagger_cdn() -> None:
    r = client.get("/docs")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == DOCS_CSP
    assert "https://cdn.jsdelivr.net" in DOCS_CSP


def test_auth_responses_are_not_cached() -> None:
    r = client.post("/auth/token", data={"username": "x@y.co", "password": "nope"})
    assert r.headers["cache-control"] == "no-store"
    assert client.post("/auth/logout").headers["cache-control"] == "no-store"


def test_hsts_only_on_https() -> None:
    assert "strict-transport-security" in headers_for("/health", "https")
    assert "strict-transport-security" not in headers_for("/health", "http")
