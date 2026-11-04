"""Acceptance tests for ticket 06 (spec S13; R11): the triage session reads its
repository through an installation token narrowed to that repository, mounted
read-only at /root/.config/gh in place of the host's gh config; inspection has
no gh mount at all.

Seam: `containers.triage_cmd` / `containers.inspection_cmd` argv, and the triage
sweep (`triage.run_sweep(cfg, deps)`, what the herdr tab `triage.tick` launches
runs as `--triage-run`) with `deps.app` a fake App. `run` is the recorded process
boundary: it snapshots the token directory mounted by the podman argv at the
moment the container would start.

Interface defined here:

* `containers.triage_cmd(..., gh_config=<host dir>)`: mounted read-only at
  /root/.config/gh; the host's ~/.config/gh is then never mounted.
* `triage.run_sweep` mints, per triage session, a token narrowed to that
  session's repository with Contents, Issues, Pull requests and Metadata at read,
  writes it as hosts.yml in a 0700 directory it owns, passes that directory as
  `gh_config`, and removes the directory when the session ends, however it ends.
"""
import re
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from dispatcher import containers, triage
from tests.test_host_installation_tokens_acceptance import FakeApp
from tests.test_triage import (BLOB, OK_USAGE, OLD, RESULT, FakeDeps, _cfg, _repository)

TRIAGE_PERMISSIONS = {"contents": "read", "issues": "read", "pull_requests": "read",
                      "metadata": "read"}
STORED_PAT = "ghp_stored_host_pat"
GH_MOUNT = "/root/.config/gh"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = Path.home()      # the suite pins it to an isolated dir
    (home / ".config" / "gh").mkdir(parents=True, exist_ok=True)
    (home / ".config" / "gh" / "hosts.yml").write_text(f"github.com:\n  oauth_token: {STORED_PAT}\n")
    (home / ".gitconfig").write_text("[user]\n\tname = agent\n")
    return home


class Boundary:
    """`run` for the sweep: answers every gh call with a failure (the board
    degrades to labels only), and for the podman session records the argv and the
    token directory as it is then, and writes the decisions file the session would."""

    def __init__(self, session="ok"):
        self.session = session          # ok | no-decisions
        self.argvs, self.tokens = [], []

    def __call__(self, argv, **kw):
        if argv[:3] != ["podman", "run", "--rm"] and "podman" not in argv[:3]:
            return subprocess.CompletedProcess(argv, 1, "", "no board")
        self.argvs.append(list(argv))
        mounts = re.findall(r"-v (\S+)", " ".join(argv))
        for mount in mounts:
            source, _, rest = mount.partition(":")
            if rest.startswith(GH_MOUNT):
                snap = {"dir": source, "hosts": None, "mode": None, "dir_mode": None}
                hosts = Path(source, "hosts.yml")
                if hosts.exists():
                    snap.update(hosts=hosts.read_text(), mode=stat.S_IMODE(hosts.stat().st_mode),
                                dir_mode=stat.S_IMODE(Path(source).stat().st_mode))
                self.tokens.append(snap)
        if self.session == "ok":
            [stem] = re.findall(r"/triage/(.+)-prompt\.md", " ".join(argv))
            triage_dir = next(m.split(":")[0] for m in mounts if m.endswith(":/triage"))
            Path(triage_dir, f"{stem}.json").write_text('{"issues": []}')
        return subprocess.CompletedProcess(argv, 0, "", "")


def sweep(tmp_path, session="ok", app=None):
    cfg = _cfg(tmp_path / "state", repositories=[_repository("analytics", "jesdi/analytics")])
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    deps = FakeDeps()
    deps.app = app or FakeApp()
    boundary = Boundary(session)
    triage.save_cursors(cfg.state_dir, {"jesdi/analytics": OLD})
    with patch.object(triage, "fetch_all", return_value=OK_USAGE), \
         patch.object(triage.triage_prefetch, "prefetch", return_value=dict(BLOB, repo="jesdi/analytics")), \
         patch.object(triage.triage_apply, "apply", return_value=RESULT):
        triage.run_sweep(cfg, deps, run=boundary)
    return cfg, deps, boundary


# --- S13: triage for jesdi/analytics ------------------------------------------------------

def test_the_triage_argv_mounts_a_token_dir_read_only_and_never_the_host_gh_config(
        tmp_path, home):
    _, _, boundary = sweep(tmp_path)
    [argv] = boundary.argvs
    line = " ".join(argv)
    [token_dir] = [t["dir"] for t in boundary.tokens]
    assert f"-v {token_dir}:{GH_MOUNT}:ro" in line
    is_host_config = token_dir == f"{home}/.config/gh"
    assert is_host_config is False
    assert f"{home}/.config/gh" not in line
    assert line.count(GH_MOUNT) == 1


def test_hosts_yml_holds_a_token_minted_for_the_one_repository_with_read_only_permissions(
        tmp_path, home):
    app = FakeApp(reached=("jesdi/analytics", "jesdi/portfolio_eval"))
    _, _, boundary = sweep(tmp_path, app=app)
    [snap] = boundary.tokens
    assert snap["hosts"] is not None
    [token] = set(re.findall(r"ghs_host_\d+", snap["hosts"]))
    [(repos, permissions, _)] = [m for m in app.mints if m[2] == token]
    assert repos == ["jesdi/analytics"]
    assert permissions == TRIAGE_PERMISSIONS
    assert STORED_PAT not in snap["hosts"]


def test_the_triage_token_dir_is_private(tmp_path, home):
    _, _, boundary = sweep(tmp_path)
    [snap] = boundary.tokens
    assert snap["mode"] is not None
    assert snap["mode"] & 0o077 == 0 and snap["dir_mode"] & 0o077 == 0


def test_triage_cmd_mounts_the_given_gh_config_dir_read_only_in_place_of_the_host_s(
        tmp_path, home, monkeypatch):
    monkeypatch.setenv("AGENT_OPS_SESSION_IMAGE", "agent-ops-session")
    tokens = tmp_path / "tok"
    tokens.mkdir()
    cmd = containers.triage_cmd("triage-jesdi-analytics", "/repos/analytics", "/triage-dir",
                                "2g", "2", "claude-fable-5", "/triage/p.md",
                                gh_config=str(tokens))
    assert f"{tokens}:{GH_MOUNT}:ro" in cmd
    assert f"{home}/.config/gh:{GH_MOUNT}:ro" not in cmd
    assert sum(GH_MOUNT in part for part in cmd) == 1


# --- S13: inspection of jesdi/billing -------------------------------------------------------

@pytest.mark.parametrize("model", ["claude-fable-5", "gpt-5.4"])
def test_an_inspection_container_has_no_gh_mount(tmp_path, home, model):
    cmd = containers.inspection_cmd("inspect-jesdi-billing", "/repos/billing",
                                    str(tmp_path / "req"), str(tmp_path / "state"),
                                    "2g", "2", model)
    assert [part for part in cmd if GH_MOUNT in part or "/.config/gh" in part] == []


# --- the token directory goes with the run ----------------------------------------------------

def test_the_token_dir_is_gone_after_the_sweep(tmp_path, home):
    cfg, _, boundary = sweep(tmp_path)
    [snap] = boundary.tokens
    assert snap["hosts"] is not None                  # it existed while the session ran
    is_host_config = snap["dir"] == str(Path.home() / ".config" / "gh")
    assert is_host_config is False
    assert not Path(snap["dir"]).exists()
    assert [p for p in Path(cfg.state_dir).rglob("hosts.yml")] == []


def test_the_token_dir_is_gone_when_the_session_fails(tmp_path, home):
    cfg, deps, boundary = sweep(tmp_path, session="no-decisions")
    [snap] = boundary.tokens
    assert snap["hosts"] is not None
    is_host_config = snap["dir"] == str(Path.home() / ".config" / "gh")
    assert is_host_config is False
    assert "FAILED" in "\n".join(deps.notifier.sent[0][1]["lines"])     # the failure path ran
    assert not Path(snap["dir"]).exists()
    assert [p for p in Path(cfg.state_dir).rglob("hosts.yml")] == []
