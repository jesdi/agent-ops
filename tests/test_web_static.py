"""Static SPA serving and the JSON fallback when dist/ is absent."""
from fastapi.testclient import TestClient

from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app


def test_json_stub_without_dist(tmp_path):
    client = TestClient(create_app(make_config(tmp_path), FakeSources()))
    r = client.get("/", headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["service"] == "agent-ops-web"


def test_serves_dist_with_spa_fallback(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>console</html>")
    (dist / "app.js").write_text("// js")
    client = TestClient(create_app(make_config(tmp_path), FakeSources(),
                                   frontend_dist=dist))
    assert "console" in client.get("/", headers=HEADERS).text
    assert client.get("/app.js", headers=HEADERS).text == "// js"
    # SPA fallback: client-side routes serve index.html
    assert "console" in client.get("/task/7", headers=HEADERS).text
    # static files still require the header
    assert client.get("/").status_code == 401
