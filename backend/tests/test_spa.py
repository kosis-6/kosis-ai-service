from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.spa import SpaFiles


def _client(directory: Path) -> TestClient:
    app = FastAPI()
    app.mount("/chat", SpaFiles(directory=str(directory)))
    return TestClient(app)


def test_serves_files_and_falls_back_to_index(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<div id=root></div>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    client = _client(tmp_path)

    for path in ("/chat/", "/chat/login", "/chat/signup/done"):
        r = client.get(path)
        assert r.status_code == 200 and "id=root" in r.text
        assert r.headers["cache-control"] == "no-cache"
    r = client.get("/chat/assets/app.js")
    assert r.status_code == 200 and "cache-control" not in r.headers
    assert client.get("/chat/assets/missing.js").status_code == 404


def test_not_built_yet(tmp_path: Path) -> None:
    client = _client(tmp_path / "dist")
    r = client.get("/chat/")
    assert r.status_code == 503 and "npm run build" in r.text
