"""Session layer: one session per task (task-<target>-<issue>), each stage
a fresh `podman run … claude` inside it. The session is the
persistence/attach/injection layer; the container is the isolation layer —
they die together at park and are recreated together at resume (claude
--continue reads transcripts from the mounted claude-home, keyed by the
worktree cwd, which is mounted at the same path inside the container).

Two backends behind one facade (spec 2026-09-03-herdr-sessions):
`_HerdrBackend` is the session layer; `_TmuxBackend` is the pre-herdr code,
kept only to finish the stage of any task that was mid-flight in tmux when
the deploy landed. Selection is adopt-on-touch (§7): a live tmux session
keeps its task on tmux until end() kills it, after which the next
spawn_stage/resume lands in herdr. The tmux backend, the probe and the
legacy-name adoption go in the retirement PR once the box reports no
`task-*` tmux session."""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path

from dispatcher import containers, herdr


def _tmux(args: list[str]) -> int:
    """Exit status of a tmux call; 1 when it cannot be made at all (binary
    absent after retirement, hung server) so the backend probe fails
    closed to herdr."""
    try:
        return subprocess.run(args, capture_output=True, timeout=30).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def session_name(target: str, issue: int) -> str:
    return f"task-{target}-{issue}"


def podman_cmd(target: str, issue: int, worktree: str, memory: str, cpus: str,
               model: str, claude_args: str) -> str:
    return containers.session_cmd(session_name(target, issue), worktree, memory,
                                  cpus, model, claude_args)


class _TmuxBackend:
    """tmux + podman, exactly as before herdr. Retire with §7."""

    def __init__(self, memory: str, cpus: str, state_dir: Path | None):
        self.memory = memory
        self.cpus = cpus
        self.state_dir = state_dir

    def exists(self, target: str, issue: int) -> bool:
        """The backend-selection probe: is there a tmux session for this
        task, under the current or the pre-rename name? Read-only — the
        rename adoption happens in _resolve on first real use."""
        return (_tmux(["tmux", "has-session", "-t", session_name(target, issue)]) == 0
                or _tmux(["tmux", "has-session", "-t", f"task-{issue}"]) == 0)

    def _resolve(self, target: str, issue: int) -> str:
        """Adopt a pre-rename tmux session on first touch: a deploy mid-flight
        must not strand live sessions under the old name."""
        name, legacy = session_name(target, issue), f"task-{issue}"
        if (_tmux(["tmux", "has-session", "-t", name]) != 0
                and _tmux(["tmux", "has-session", "-t", legacy]) == 0):
            _tmux(["tmux", "rename-session", "-t", legacy, name])
        return name

    def is_alive(self, target: str, issue: int) -> bool:
        name = self._resolve(target, issue)
        if _tmux(["tmux", "has-session", "-t", name]) == 0:
            return True
        # Adoption above renames on success, but a caller must still read
        # "alive" true if the legacy session exists and, for any reason
        # (e.g. a racing rename), wasn't adopted on this call.
        return _tmux(["tmux", "has-session", "-t", f"task-{issue}"]) == 0

    def launch(self, target: str, issue: int, worktree: str, model: str,
               claude_args: str) -> None:
        name = self._resolve(target, issue)
        if _tmux(["tmux", "has-session", "-t", name]) != 0:
            _tmux(["tmux", "new-session", "-d", "-s", name, "-c", worktree])
        cmd = podman_cmd(target, issue, worktree, self.memory, self.cpus, model,
                         claude_args)
        _tmux(["tmux", "send-keys", "-t", name, cmd, "Enter"])

    def capture_tail(self, target: str, issue: int, lines: int = 25) -> str:
        name = self._resolve(target, issue)
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", name],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return ""
        return "\n".join(out.stdout.rstrip().splitlines()[-lines:])

    def capture_history(self, target: str, issue: int, lines: int = 2000) -> str:
        """Scrollback via -S so the web console can render true history.
        Degrades to '' on tmux failure exactly as capture_tail does."""
        name = self._resolve(target, issue)
        out = subprocess.run(
            ["tmux", "capture-pane", "-p", "-S", f"-{lines}", "-t", name],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return ""
        return out.stdout.rstrip()

    def idle_seconds(self, target: str, issue: int) -> float | None:
        """Seconds since the task's tmux window last changed. Claude Code's
        status line redraws every second while working, so a live session
        reads near zero; only a truly static screen accumulates idle time.
        None = unknown (tmux error) — callers must not treat it as stalled."""
        name = self._resolve(target, issue)
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", name,
             "#{window_activity}"],
            capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        try:
            return max(0.0, time.time() - int(out.stdout.strip()))
        except ValueError:
            return None

    def send_text(self, target: str, issue: int, text: str) -> None:
        """-l stops tmux interpreting the text as key names; Enter goes
        separately because -l would type the word 'Enter' literally."""
        name = self._resolve(target, issue)
        _tmux(["tmux", "send-keys", "-t", name, "-l", text])
        _tmux(["tmux", "send-keys", "-t", name, "Enter"])

    def end(self, target: str, issue: int) -> None:
        name = self._resolve(target, issue)
        _tmux(["tmux", "kill-session", "-t", name])
        subprocess.run(["podman", "rm", "-f", name],
                       capture_output=True, timeout=60)
        # Containers created pre-deploy are named task-<issue> (no target),
        # and unlike the tmux session above nothing ever renames them on
        # adoption — so a wedged legacy container is never targeted again
        # and leaks until reboot. Best-effort, same as the call above: safe
        # forever, since a bare task-<issue> name cannot exist post-migration.
        subprocess.run(["podman", "rm", "-f", f"task-{issue}"],
                       capture_output=True, timeout=60)


class _HerdrBackend:
    """herdr server as the session layer (spec 2026-09-03 §2): a workspace
    per target, a tab per task labelled task-<target>-<issue>, and the tab's
    root pane hosting `podman run … claude`. IDs are resolved by label on
    every call and never stored; a missing tab is "dead", as a missing tmux
    session was. Every herdr failure degrades exactly as a tmux failure
    did: alive → False, capture → "", mutations best-effort."""

    def __init__(self, memory: str, cpus: str, state_dir: Path | None):
        self.memory = memory
        self.cpus = cpus
        self.state_dir = state_dir

    def _pane(self, target: str, issue: int) -> str | None:
        found = herdr.tab(session_name(target, issue))
        if found is None:
            return None
        return herdr.root_pane(*found)

    def is_alive(self, target: str, issue: int) -> bool:
        # A pane back at the host shell after claude exits still reads alive
        # — identical to tmux (main.py's _inject_login_code relies on it).
        return herdr.tab(session_name(target, issue)) is not None

    def launch(self, target: str, issue: int, worktree: str, model: str,
               claude_args: str) -> None:
        name = session_name(target, issue)
        pane = self._pane(target, issue)
        if pane is None:
            workspace = herdr.ensure_workspace(
                target, containers.clone_root(worktree))
            if workspace is None:
                return  # server down: the task reads dead, the crash path owns it
            pane = herdr.create_tab(workspace, name, worktree)
            if pane is None:
                return
        # HERDR_AGENT=claude is herdr's hint for detecting an agent behind a
        # wrapper (with-claude-token.sh execs podman with it inherited); it
        # is a herdr concern, so it lives here and not in containers.py —
        # the headless triage container shares that module and must NOT be
        # detected as an agent.
        cmd = "HERDR_AGENT=claude " + podman_cmd(
            target, issue, worktree, self.memory, self.cpus, model, claude_args)
        herdr.run_command(pane, cmd)

    def capture_tail(self, target: str, issue: int, lines: int = 25) -> str:
        pane = self._pane(target, issue)
        text = herdr.read(pane, "visible", lines) if pane else None
        if text is None:
            return ""
        return "\n".join(text.rstrip().splitlines()[-lines:])

    def capture_history(self, target: str, issue: int,
                        lines: int = 2000) -> str:
        # recent-unwrapped joins soft wraps — true history for the console,
        # unlike tmux's hard-wrapped scrollback.
        pane = self._pane(target, issue)
        text = herdr.read(pane, "recent-unwrapped", lines) if pane else None
        return "" if text is None else text.rstrip()

    def idle_seconds(self, target: str, issue: int) -> float | None:
        """Seconds the session has been static; None = unknown. Computed
        from the agent lifecycle, not screen activity: `working` is never
        idle, however long it lasts; any other status (idle / blocked /
        done / unknown, or no agent at all — the pane back at the host
        shell) accumulates from the moment herdr last changed its mind.
        herdr exposes the transition counter but no timestamp, so the
        moment is remembered in a sidecar keyed by (seq, status)."""
        pane = self._pane(target, issue)
        state = herdr.agent_state(pane) if pane else None
        if state is None:
            return None
        status, seq = state
        if status == "working":
            return 0.0
        return self._since_change(target, issue, status, seq)

    def _sidecar(self, target: str, issue: int) -> Path | None:
        if self.state_dir is None:
            return None
        return (self.state_dir / "herdr-status"
                / f"{session_name(target, issue)}.json")

    def _since_change(self, target: str, issue: int, status: str,
                      seq: int) -> float | None:
        path = self._sidecar(target, issue)
        if path is None:
            return None
        now = time.time()
        try:
            try:
                stored = json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                stored = None  # absent or corrupt: start the clock afresh
            if (isinstance(stored, dict) and stored.get("seq") == seq
                    and stored.get("status") == status):
                return max(0.0, now - float(stored["since"]))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"seq": seq, "status": status, "since": now}))
            return 0.0
        except (OSError, KeyError, TypeError, ValueError):
            return None  # unknown, never a stale number

    def forget_status(self, target: str, issue: int) -> None:
        path = self._sidecar(target, issue)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def send_text(self, target: str, issue: int, text: str) -> None:
        pane = self._pane(target, issue)
        if pane is None:
            return
        herdr.send_text(pane, text)
        herdr.send_keys(pane, "enter")

    def end(self, target: str, issue: int) -> None:
        name = session_name(target, issue)
        found = herdr.tab(name)
        if found is not None:
            herdr.close_tab(found[1])  # HUPs the shell and the -it podman
        # Belt and braces, as with tmux: the container may outlive the HUP.
        subprocess.run(["podman", "rm", "-f", name],
                       capture_output=True, timeout=60)
        # Pre-rekey containers are named task-<issue>; safe forever, see
        # _TmuxBackend.end for the history.
        subprocess.run(["podman", "rm", "-f", f"task-{issue}"],
                       capture_output=True, timeout=60)
        self.forget_status(target, issue)


class Sessions:
    """The dispatcher's and the console's one session interface. Dry-run
    never touches a backend; everything else resolves the backend per call."""

    def __init__(self, dry_run: bool = False, memory: str = "2g",
                 cpus: str = "2", state_dir: str | Path | None = None):
        self.dry_run = dry_run
        self.memory = memory
        self.cpus = cpus
        self.state_dir = Path(state_dir) if state_dir else None
        self._tmux_backend = _TmuxBackend(memory, cpus, self.state_dir)
        self._herdr_backend = _HerdrBackend(memory, cpus, self.state_dir)

    def _backend(self, target: str, issue: int):
        """Adopt-on-touch (spec §7): a session that predates the deploy is
        a tmux one and stays there until end() kills it; everything else —
        every new launch, every resume, every parked task — is herdr. One
        extra local probe per operation while tmux is installed; absent
        binary fails closed to herdr."""
        if self._tmux_backend.exists(target, issue):
            return self._tmux_backend
        return self._herdr_backend

    def is_alive(self, target: str, issue: int) -> bool:
        return self._backend(target, issue).is_alive(target, issue)

    def spawn_stage(self, target: str, issue: int, worktree: str, prompt: str,
                    stage_name: str, model: str) -> None:
        if self.dry_run:
            print(f"[dry-run] spawn stage '{stage_name}' on {model} in session "
                  f"{session_name(target, issue)} at {worktree}")
            return
        agent_dir = Path(worktree) / ".agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / f"prompt-{stage_name}.md").write_text(prompt)
        self._backend(target, issue).launch(
            target, issue, worktree, model,
            f'"$(cat .agent/prompt-{stage_name}.md)"')

    def resume(self, target: str, issue: int, worktree: str, message: str,
               model: str) -> None:
        if self.dry_run:
            print(f"[dry-run] resume {session_name(target, issue)} on {model} "
                  f"at {worktree}")
            return
        self._backend(target, issue).launch(
            target, issue, worktree, model,
            f"--continue {shlex.quote(message)}")

    def capture_tail(self, target: str, issue: int, lines: int = 25) -> str:
        if self.dry_run:
            return ""
        return self._backend(target, issue).capture_tail(target, issue, lines)

    def capture_history(self, target: str, issue: int, lines: int = 2000) -> str:
        """Console-owned pane history (true scrollback, unlike capture_tail —
        the dispatcher's stall/login classification input). Degrades to ''
        so a wedged server never 500s the history view."""
        if self.dry_run:
            return ""
        return self._backend(target, issue).capture_history(target, issue, lines)

    def idle_seconds(self, target: str, issue: int) -> float | None:
        """Seconds the session has been static; None = unknown (dry-run,
        backend error) — callers must not treat it as stalled."""
        if self.dry_run:
            return None
        return self._backend(target, issue).idle_seconds(target, issue)

    def send_text(self, target: str, issue: int, text: str) -> None:
        """Type text into the live pane as-is (login-code injection), then
        Enter as a separate key."""
        if self.dry_run:
            print(f"[dry-run] send text to {session_name(target, issue)}")
            return
        self._backend(target, issue).send_text(target, issue, text)

    def _snapshot(self, backend, target: str, issue: int) -> None:
        """Last words: persist the pane history before the kill so the
        console can still show a dead session's output. An empty capture
        (already-dead session, wedged server) writes nothing — end() runs
        again on dead sessions and must not truncate the snapshot the park
        wrote. A failed write must never keep the session alive."""
        if self.state_dir is None:
            return
        try:
            history = backend.capture_history(target, issue)
            if not history:
                return
            path = (self.state_dir / "snapshots" /
                    f"{session_name(target, issue)}.txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(history)
        except (OSError, subprocess.SubprocessError):
            return

    def end(self, target: str, issue: int) -> None:
        if self.dry_run:
            print(f"[dry-run] end session {session_name(target, issue)}")
            return
        backend = self._backend(target, issue)
        self._snapshot(backend, target, issue)
        backend.end(target, issue)
