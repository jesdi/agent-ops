import json
import subprocess
import sys

from tests.webfakes import FakeSources, make_config
from web.app import create_app


def test_openapi_export_prints_schema_with_contract_paths():
    out = subprocess.run(
        [sys.executable, "-m", "web.openapi"],
        check=True, capture_output=True, text=True,
    ).stdout
    schema = json.loads(out)
    paths = schema["paths"]
    for p in ("/api/board", "/api/queue", "/api/budget", "/api/failures",
              "/api/history", "/api/pending-intents",
              "/api/task/{target}/{issue}"):
        assert p in paths, f"missing {p}"
    names = schema["components"]["schemas"]
    for n in ("BoardView", "QueueView", "TaskDetail", "BudgetView",
              "FailuresView", "HistoryView"):
        assert n in names, f"missing schema {n}"


def test_schema_is_identical_with_and_without_a_built_frontend(tmp_path):
    """The committed api-types.ts is checked for drift in CI, where codegen
    runs before `pnpm build`. If the dist/-absent stub route reached the
    schema, generating with and without a build would disagree and the
    freshness check would fail on a clean tree."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>console</html>")

    def schema_for(frontend_dist):
        return create_app(make_config(tmp_path), FakeSources(),
                          frontend_dist=frontend_dist).openapi()

    assert schema_for(dist) == schema_for(tmp_path / "nonexistent")
