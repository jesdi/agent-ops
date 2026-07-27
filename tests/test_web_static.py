"""Static SPA serving and the JSON fallback when dist/ is absent."""
from fastapi.testclient import TestClient

from tests.webfakes import FakeSources, HEADERS, make_config
from web.app import create_app


def test_json_stub_without_dist(tmp_path):
    client = TestClient(create_app(make_config(tmp_path), FakeSources(),
                                   frontend_dist=tmp_path / "nonexistent"))
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


def test_unknown_api_path_is_404_not_the_spa_shell(tmp_path):
    """With dist/ mounted the SPA fallback used to answer /api/typo with
    200 text/html, which surfaces in the frontend as a parse error rather
    than a missing route."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>console</html>")
    client = TestClient(create_app(make_config(tmp_path), FakeSources(),
                                   frontend_dist=dist))
    r = client.get("/api/tasks", headers=HEADERS)
    assert r.status_code == 404
    assert "console" not in r.text
    # real API routes and client-side routes are unaffected
    assert client.get("/api/health", headers=HEADERS).status_code == 200
    assert "console" in client.get("/task/7", headers=HEADERS).text
