"""Print the app's OpenAPI schema as JSON on stdout.

Consumed by frontend codegen (`pnpm gen:api` -> openapi-typescript). The
schema depends only on route and response-model declarations, so stubbed
sources are fine: no I/O happens at schema-build time.
"""
import json
from unittest.mock import MagicMock

from dispatcher.config import Board, Config, Project, Repository
from web.app import create_app


def _minimal_config() -> Config:
    board = Board(owner="jesdi", number=1, status_field_id="F",
                  status_ready_option_id="R", status_in_progress_option_id="P",
                  boost_field_id="B")
    repository = Repository(
        name="alpha", repo="jesdi/alpha", github_id=1,
        clone_path="/tmp/clones/alpha",
        worktrees_path="/tmp/worktrees/alpha",
        project=Project(name="alpha", board=board),
    )
    return Config(
        state_dir="/tmp/agent-ops-openapi", capacity=2,
        session_memory="2g", session_cpus="2", repositories=[repository],
    )


def main() -> None:
    app = create_app(_minimal_config(), MagicMock())
    print(json.dumps(app.openapi(), sort_keys=True))


if __name__ == "__main__":
    main()
