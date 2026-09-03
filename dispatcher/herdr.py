"""herdr socket-CLI adapter. One `herdr …` subprocess per call, JSON reply
parsed, and one degrade contract throughout: any failure — a
non-zero exit (server error, JSON on stdout, exit 1; CLI syntax error,
plain text, exit 2), a timeout, a missing binary, an unparseable reply —
reads as None / False, and mutations are best-effort.

IDs (`w1`, `w1:t3`, `w1:p3`) are opaque and are never persisted: `Tab` is
the way callers resolve by label on every operation (workspace = target,
tab = task-<target>-<issue>) rather than holding an id. This module
knows nothing about tasks;
dispatcher/sessions.py and triage.py own that mapping."""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

TIMEOUT = 30
SYSTEM_WORKSPACE = "agent-ops"


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    """One CLI call; None when it could not even be made (no binary, hung
    server). Callers read returncode/stdout from the CompletedProcess."""
    try:
        return subprocess.run(["herdr", *args], capture_output=True,
                              text=True, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def _ok(proc: subprocess.CompletedProcess | None) -> bool:
    return proc is not None and proc.returncode == 0


def _result(proc: subprocess.CompletedProcess | None) -> dict | None:
    """The `result` object of a successful reply, else None."""
    if not _ok(proc):
        return None
    try:
        result = json.loads(proc.stdout)["result"]
    except (ValueError, KeyError, TypeError):
        return None
    return result if isinstance(result, dict) else None


def _error_code(proc: subprocess.CompletedProcess | None) -> str:
    """`error.code` of a failed reply; "" when there is none to read."""
    if proc is None:
        return ""
    try:
        return str(json.loads(proc.stdout)["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return ""


def _call(args: list[str]) -> dict | None:
    return _result(_run(args))


# -- topology: workspace / tab / pane by label ---------------------------------

def workspace(label: str) -> str | None:
    """workspace_id of the workspace labelled `label`."""
    result = _call(["workspace", "list"])
    if result is None:
        return None
    for ws in result.get("workspaces") or []:
        if isinstance(ws, dict) and ws.get("label") == label:
            return ws.get("workspace_id")
    return None


def ensure_workspace(label: str, cwd: str) -> str | None:
    """workspace_id of `label`, creating it (unfocused, at `cwd`) when absent.
    Workspaces are created on first use and never closed by the dispatcher."""
    found = workspace(label)
    if found is not None:
        return found
    result = _call(["workspace", "create", "--label", label, "--cwd", cwd,
                    "--no-focus"])
    try:
        return result["workspace"]["workspace_id"]
    except (KeyError, TypeError):
        return None


def tab(label: str) -> tuple[str, str] | None:
    """(workspace_id, tab_id) of the first tab labelled `label`. `tab list`
    without --workspace is server-wide, so one call covers every target."""
    result = _call(["tab", "list"])
    if result is None:
        return None
    for t in result.get("tabs") or []:
        if isinstance(t, dict) and t.get("label") == label:
            try:
                return str(t["workspace_id"]), str(t["tab_id"])
            except KeyError:
                return None
    return None


def root_pane(workspace_id: str, tab_id: str) -> str | None:
    """pane_id of the tab's root pane — the pane whose tab_id matches; the
    dispatcher never splits its tabs, so the first match is the only one."""
    result = _call(["pane", "list", "--workspace", workspace_id])
    if result is None:
        return None
    for p in result.get("panes") or []:
        if isinstance(p, dict) and p.get("tab_id") == tab_id:
            return p.get("pane_id")
    return None


def create_tab(workspace_id: str, label: str, cwd: str,
               env: dict[str, str] | None = None) -> str | None:
    """Create an unfocused tab at `cwd`; returns its root pane_id. `env`
    becomes `--env K=V` pairs for the launched shell."""
    args = ["tab", "create", "--workspace", workspace_id, "--label", label,
            "--cwd", cwd]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    result = _call(args + ["--no-focus"])
    try:
        return result["root_pane"]["pane_id"]
    except (KeyError, TypeError):
        return None


# -- pane I/O ------------------------------------------------------------------

def read(pane_id: str, source: str, lines: int) -> str | None:
    """Pane text. `pane read` prints the snapshot itself (plain text, ANSI
    stripped), not a JSON envelope — so this is the one call whose stdout is
    returned as-is."""
    proc = _run(["pane", "read", pane_id, "--source", source,
                 "--lines", str(lines)])
    if not _ok(proc):
        return None
    return proc.stdout


def run_command(pane_id: str, command: str) -> bool:
    """Type `command` and Enter into the pane's shell (one atomic call)."""
    return _ok(_run(["pane", "run", pane_id, command]))


def send_text(pane_id: str, text: str) -> bool:
    return _ok(_run(["pane", "send-text", pane_id, text]))


def send_keys(pane_id: str, *keys: str) -> bool:
    return _ok(_run(["pane", "send-keys", pane_id, *keys]))


def close_tab(tab_id: str) -> bool:
    return _ok(_run(["tab", "close", tab_id]))


def pane_busy(pane_id: str) -> bool | None:
    """True while something runs in the pane's shell: the foreground process
    group is not the shell itself. False at the prompt. None when herdr
    could not say. This is how a tab restored after a server restart (a
    fresh shell wearing the old label) is told apart from a live one."""
    result = _call(["pane", "process-info", "--pane", pane_id])
    try:
        info = result["process_info"]
        return int(info["foreground_process_group_id"]) != int(info["shell_pid"])
    except (KeyError, TypeError, ValueError):
        return None


# -- agent lifecycle -----------------------------------------------------------

def agent_state(pane_id: str) -> tuple[str, int] | None:
    """(agent_status, state_change_seq) for the agent herdr detects in the
    pane. ("none", 0) when it detects none — the pane is back at the host
    shell, or the container has not reached claude yet — which is a real
    state, not an error. None when the server could not be asked (down,
    hung, syntax error, garbage reply): callers treat it as unknown."""
    proc = _run(["agent", "get", pane_id])
    result = _result(proc)
    if result is not None:
        try:
            agent = result["agent"]
            return (str(agent["agent_status"]),
                    int(agent.get("state_change_seq", 0)))
        except (KeyError, TypeError, ValueError):
            return None
    if _error_code(proc) == "agent_not_found":
        return "none", 0
    return None


# -- Tab: one labelled tab hosting one command ---------------------------------

# Aliases so the Tab class methods call these module functions by unambiguous
# names (read, agent_state, send_text, send_keys each share their name with a
# Tab method; Python's LEGB rule would resolve them to the module globals
# inside method bodies regardless, but these aliases make the intent explicit).
_read_pane = read
_agent_state_pane = agent_state
_send_text_pane = send_text
_send_keys_pane = send_keys


@dataclass(frozen=True)
class Tab:
    """A labelled tab and its root pane, resolved by label once per
    operation and never persisted. `label` is the only identity; the ids
    are a snapshot of the server's current numbering."""
    label: str
    workspace_id: str
    tab_id: str
    pane_id: str

    @classmethod
    def find(cls, label: str) -> "Tab | None":
        """The tab labelled `label`, or None when absent or the server
        cannot be asked. One `tab list` + one `pane list`."""
        loc = tab(label)
        if loc is None:
            return None
        ws, tab_id = loc
        pane = root_pane(ws, tab_id)
        if pane is None:
            return None
        return cls(label, ws, tab_id, pane)

    @classmethod
    def ensure(cls, workspace_label: str, label: str, cwd: str,
               env: dict[str, str] | None = None) -> "Tab | None":
        """A tab under `label` whose shell can take a command: an existing
        busy tab (never killed — the caller decides what to do with live
        work); otherwise a fresh one. An existing tab that is NOT busy
        (a finished command at its prompt, or a fresh shell restored after
        a server restart wearing the old label) is closed first so the
        label never carries two tabs and a restored tab never hosts a new
        launch without `env`. Creates the workspace on first use (at
        `cwd`'s clone root is the caller's concern — pass the cwd the
        workspace should open at as `cwd` when creating; this function
        passes the same `cwd` to both). None when the server is down or
        creation failed."""
        existing = cls.find(label)
        if existing is not None:
            if existing.alive:
                return existing
            existing.close()  # best-effort; fall through to create a fresh tab
        ws = ensure_workspace(workspace_label, cwd)
        if ws is None:
            return None
        pane = create_tab(ws, label, cwd, env)
        if pane is None:
            return None
        return cls.find(label)  # re-resolve: create_tab returns only the pane id

    @property
    def alive(self) -> bool:
        """Exists (this object was resolved) AND its shell is busy — the
        foreground process group is not the shell. Unknown busyness reads
        alive (fail closed: a false "dead" fires the crash path or a
        duplicate sweep). A restored tab is a bare shell -> not alive."""
        return pane_busy(self.pane_id) is not False

    def run(self, command: str) -> bool:
        """Type `command` and Enter into the pane's shell."""
        return run_command(self.pane_id, command)

    def read(self, source: str, lines: int) -> str | None:
        """Pane text snapshot."""
        return _read_pane(self.pane_id, source, lines)

    def agent_state(self) -> tuple[str, int] | None:
        """Agent status and state-change sequence for the pane."""
        return _agent_state_pane(self.pane_id)

    def send_text(self, text: str) -> bool:
        """Send raw text to the pane."""
        return _send_text_pane(self.pane_id, text)

    def send_keys(self, *keys: str) -> bool:
        """Send key sequences to the pane."""
        return _send_keys_pane(self.pane_id, *keys)

    def close(self) -> bool:
        """Close the tab."""
        return close_tab(self.tab_id)
