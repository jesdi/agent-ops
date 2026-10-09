"""Real listener fixture for tests that fake the physical terminal boundary."""
import tempfile
import threading
from pathlib import Path

import pytest

from dispatcher import runtime_http, waitd


@pytest.fixture
def launch_listener(monkeypatch):
    servers = []
    original = runtime_http.UnixHTTP.__init__
    sockets = {}
    with tempfile.TemporaryDirectory(prefix="launch-", dir="/tmp") as temp:
        def connect_init(self, state_dir):
            original(self, state_dir)
            key = str(state_dir)
            if key not in sockets:
                path = Path(temp) / str(len(servers))
                runtime_http.ensure_credential(state_dir)
                server = waitd._Server(path, state_dir)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                servers.append((server, thread))
                sockets[key] = str(path)
            self.path = sockets[key]
        monkeypatch.setattr(runtime_http.UnixHTTP, "__init__", connect_init)
        # Credential is read after connection construction, so this starts the
        # actual writer before the client loads its host credential.
        try:
            yield
        finally:
            for server, thread in servers:
                server.shutdown()
                thread.join()
                server.server_close()


def seed_resume_task(state_dir, worktree):
    from dispatcher.state import TaskState, Stage, save
    save(state_dir, TaskState(target="acme", issue=42, stage=Stage.REVIEW,
                             worktree=worktree, slot=1, branch="agent/task-42",
                             title="Test", updated_at="2026-10-06T00:00:00+00:00"))
