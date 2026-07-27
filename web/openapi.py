"""Print the app's OpenAPI schema as JSON on stdout.

Consumed by frontend codegen (`pnpm gen:api` -> openapi-typescript). The
schema depends only on route and response-model declarations, so stubbed
sources are fine: no I/O happens at schema-build time.
"""
import json
from unittest.mock import MagicMock

from dispatcher.config import Config, Target
from web.app import create_app


def _minimal_config() -> Config:
    target = Target(
        name="alpha", repo="jesdi/alpha",
        clone_path="/tmp/clones/alpha",
        worktrees_path="/tmp/worktrees/alpha",
        rank_cmd="false", setup_cmd="", verify_cmd="",
        project_number=1, project_owner="jesdi",
        status_field_id="F", status_ready_option_id="R",
        status_in_progress_option_id="P", boost_field_id="B",
    )
    return Config(
        state_dir="/tmp/agent-ops-openapi", capacity=2,
        budget_threshold=0.8, racing_minutes=30, racing_threshold=0.95,
        session_memory="2g", session_cpus="2", targets=[target],
    )


def main() -> None:
    app = create_app(_minimal_config(), MagicMock())
    print(json.dumps(app.openapi(), sort_keys=True))


if __name__ == "__main__":
    main()
