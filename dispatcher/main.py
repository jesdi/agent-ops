"""One dispatcher pass: candidates → capacity → budget → claim → drive.

Stateless per pass — the view is rebuilt every time from the board (via
candidates), state files, stage.json signals, and session liveness. Run by a
systemd timer; one invocation = one pass."""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import traceback
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from dispatcher.convergence import pass_lock
from dispatcher.config import (Config, Repository, apply_stored_models,
                               load_config, referenced_providers,
                               routed_providers)
from dispatcher.usage import admits
from dispatcher.usage_providers import ADAPTERS, fetch_all
from dispatcher import (failures, github_app, host_auth, priority,
                        tmux_migration, triage, onboarding_sessions)
from dispatcher.deps import Admit, Deps, _access, _now
from dispatcher.github import GitHubClient
from dispatcher import migrate
from dispatcher.models import Order
from dispatcher.sessions import Sessions
from dispatcher.state import TaskState, load_all
from dispatcher.claims import box_free
from telegram.notify import Notifier
from dispatcher.drive import (_admission_candidates, _drive_task, _driven, _flush_finished,
                             _wake_reached, _woken)
from dispatcher.executor import _fail_task_crash
from dispatcher.claiming import _claim_new, _reconcile_boards
from dispatcher.pr_watch import _poll_prs, _wake_ci
from dispatcher.operator_intents import _apply_intents
from dispatcher.stage_launch import _sync_task_tokens, _wake
from dispatcher.operator_chat import _handle_telegram, _status_lines
from dispatcher.upkeep import (_auth_dark_edge, _budget_edge, _migrate_legacy_keys,
                               _prune_snapshots, _reconcile_wake_blocked,
                               _sync_artifacts, _write_heartbeat)


@contextmanager
def _sandboxed_state(cfg: Config):
    """Yield `cfg` with state_dir rebound to a throwaway copy of the real one.

    --dry-run stubs every I/O *dependency* (GitHubClient, Sessions, Notifier,
    remove_workspace), but the pass also writes locally — state files, the
    event log, waiting markers, failure reports, the intent queue,
    the per-provider usage cache — and those writes were unguarded, so a dry run advanced
    tasks to terminal stages and wrote history for real. Guarding each call
    site is what already rotted: `dry_run` is threaded by hand into some
    functions and was never added to the flush or the wake path.

    Redirecting the one directory they all write through is the guard that
    cannot be forgotten, and it covers write sites added later for free. The
    pass still reads a faithful copy, so its decisions — and the [dry-run]
    lines it prints — are exactly the ones a real pass would make."""
    real = Path(cfg.state_dir)
    with tempfile.TemporaryDirectory(prefix="agent-ops-dry-run-") as tmp:
        sandbox = Path(tmp) / "state"
        if real.is_dir():
            shutil.copytree(real, sandbox)
        else:
            sandbox.mkdir(parents=True)
        yield replace(cfg, state_dir=str(sandbox))



def run_pass(cfg: Config, deps: Deps, dry_run: bool = False,
             config_path: str = "targets.yaml") -> None:
    if dry_run:
        with _sandboxed_state(cfg) as sandboxed:
            _run_pass(sandboxed, deps, dry_run, config_path)
        return
    _run_pass(cfg, deps, dry_run, config_path)


def _begin_pass_auth(cfg: Config, deps: Deps) -> None:
    """The pass's App access and the host credentials it gives each project."""
    deps.access = _access(deps)
    projects: dict[str, list[str]] = {}
    for r in cfg.repositories:
        projects.setdefault(r.project.name, []).append(r.repo)
    deps.github.auth = host_auth.begin(list(projects.values()), deps.access)


def _run_pass(cfg: Config, deps: Deps, dry_run: bool = False,
              config_path: str = "targets.yaml") -> None:
    pass_started = _now()
    _begin_pass_auth(cfg, deps)
    _migrate_legacy_keys(cfg)
    _reconcile_wake_blocked(cfg)
    if not dry_run:
        _housekeep(cfg, deps, config_path)
    # Evaluate each triage signal once — triage.running() is a real herdr
    # subprocess; calling it per repository would spawn (1 + len(repositories)) processes
    # each pass. The two signals are semantically distinct: eff is reduced when
    # the sweep is running (it holds a real capacity unit occupies() cannot see);
    # claims are paused when any request is in flight (pending = file OR running).
    sweep_running = triage.running()
    occupied = int(sweep_running) + onboarding_sessions.running(cfg.state_dir)
    eff = replace(cfg, capacity=max(0, cfg.capacity - occupied))
    claims_paused = triage.pending(cfg.state_dir)
    _handle_telegram(cfg, deps, dry_run)
    usages = fetch_all(cfg)
    now = datetime.now(timezone.utc)
    admit: Admit = lambda model: admits(usages, model, now, cfg.pace)
    # Ranked on the reading the gate judges; the mode is read once per pass,
    # so a change applies from the next one.
    order = priority.order(priority.load(cfg.state_dir, routed_providers(cfg)),
                           usages, now, cfg.pace)
    default_verdict = admit(cfg.models.gate_entry().model_id)
    _budget_edge(cfg, deps, default_verdict, now)
    _auth_dark_edge(cfg, deps, usages)
    # Phase 1: work already in progress, on every repository, before any claim —
    # so one repository's new claims never starve another's approved spec.
    _drive_in_progress(eff, deps, admit, order, dry_run)
    _wake_reached(eff, deps)
    _drive_wakes(eff, deps, admit, order, dry_run)
    if not dry_run:
        launched = onboarding_sessions.tick(
            cfg, deps, admit, order, config_path,
            free=box_free(eff.capacity, load_all(cfg.state_dir), triage_running=False))
        eff = replace(eff, capacity=max(0, eff.capacity - launched))
        _reconcile_boards(cfg, deps)
    # Phase 2: new claims, with whatever capacity phase 1 and inspection left.
    if not claims_paused:
        _claim_new(eff, deps, eff.repositories, admit, order, dry_run, pass_started)
    _sync_artifacts(cfg, dry_run=dry_run, auth=deps.github.auth)
    _flush_finished(cfg, deps, dry_run)
    _write_heartbeat(cfg, pass_started)


def _housekeep(cfg: Config, deps: Deps, config_path: str) -> None:
    """A real pass's upkeep before any work: tokens, intents, snapshots,
    onboarding sessions, and the triage sweep on what they leave."""
    _sync_task_tokens(cfg, deps)
    _apply_intents(cfg, deps)
    _prune_snapshots(cfg)
    onboarding_sessions.reconcile(cfg.state_dir)
    reserved = onboarding_sessions.running(cfg.state_dir)
    triage.tick(replace(cfg, capacity=max(0, cfg.capacity - reserved)), deps, config_path)


def _drive_in_progress(cfg: Config, deps: Deps, admit: Admit, order: Order,
                       dry_run: bool) -> None:
    """Drive every repository's in-progress tasks, then its CI wakes and PRs."""
    for repository in cfg.repositories:
        for task in [t for t in load_all(cfg.state_dir)
                     if t.repository == repository.name and _driven(t)]:
            _drive_guarded(cfg, deps, repository, task, admit, order, dry_run)
        _wake_ci(cfg, deps, repository)
        _poll_prs(cfg, deps, repository, dry_run)


def _drive_wakes(cfg: Config, deps: Deps, admit: Admit, order: Order,
                 dry_run: bool = False) -> None:
    """Drive every waking task box-wide, oldest first: capacity is shared,
    so repository listing order must not decide whose wake waits."""
    for repository, task in _admission_candidates(cfg, _woken):
        _drive_guarded(cfg, deps, repository, task, admit, order, dry_run)


def _drive_guarded(cfg: Config, deps: Deps, repository: Repository, task: TaskState,
                   admit: Admit, order: Order, dry_run: bool) -> None:
    """Drive one task's turn; a crash fails that task, not the pass."""
    try:
        _drive_task(cfg, deps, repository, task, admit, order, dry_run)
    except host_auth.HostWait as exc:   # the turn waits for the App, not a crash
        print(f"[warn] #{task.issue}: {exc}; its turn waits for the next pass",
              file=sys.stderr)
    except Exception:
        _fail_task_crash(cfg, deps, repository, task, dry_run)


def guarded_pass(cfg: Config, deps: Deps, config_path: str,
                 dry_run: bool = False) -> None:
    try:
        run_pass(cfg, deps, dry_run=dry_run, config_path=config_path)
    except Exception:
        rep = failures.FailureReport(
            klass="pass-crash", repository="", issue=0, title="(dispatcher)",
            error=traceback.format_exc(), log_tail="",
            repro=f"agent-ops-dispatcher --config {config_path}",
            worktree="")
        failures.report_failure(cfg, deps, rep, dry_run=dry_run)
        raise  # systemd must still see the unit fail


def send_digest(cfg: Config, deps: Deps) -> None:
    deps.notifier.send("daily_digest", lines=_status_lines(cfg))


def _load(config_path: str) -> tuple[Config, Config]:
    """targets.yaml as loaded, and that with the console's stored model
    settings in force; warns when the stored ones are rejected."""
    boot = load_config(config_path)
    cfg = apply_stored_models(boot)
    if cfg.models_origin.error:
        print(f"[warn] {cfg.models_origin.error}", file=sys.stderr)
    return boot, cfg


def _warn_unadapted(cfg: Config) -> None:
    missing = referenced_providers(cfg) - set(ADAPTERS)
    if missing:
        print(f"[warn] models: provider(s) {sorted(missing)} have no usage adapter; "
              f"their entries are never admitted", file=sys.stderr)


def _enqueue_triage(cfg: Config) -> None:
    if triage.enqueue(cfg.state_dir):
        print("triage request enqueued")
    else:
        print("triage already pending or running; not enqueued")


def _deps(cfg: Config, dry_run: bool) -> Deps:
    return Deps(github=GitHubClient(dry_run=dry_run), app=github_app.Client(),
                sessions=Sessions(dry_run=dry_run, memory=cfg.session_memory,
                                  cpus=cfg.session_cpus, state_dir=cfg.state_dir,
                                  clones={t.name: t.clone_path for t in cfg.repositories}),
                notifier=Notifier(dry_run=dry_run,
                                  console_url=cfg.console_url,
                                  multi_repository=len(cfg.repositories) > 1))


def _migrate_tmux(cfg: Config) -> None:
    """Called by agent-ops-infra/provision/update.sh, which already holds
    convergence.lock (the same file pass_lock flocks — taking it here again
    would block forever, flock being per open-file-description). Never run
    by hand while the dispatcher timer is live."""
    for line in tmux_migration.migrate(
            cfg.state_dir, lambda task, text: _wake(cfg, task, text)):
        print(line)


def main() -> None:
    ap = argparse.ArgumentParser(prog="agent-ops-dispatcher")
    ap.add_argument("--config", default="targets.yaml")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--triage", action="store_true")
    ap.add_argument("--triage-run", action="store_true")
    ap.add_argument("--migrate-tmux", action="store_true")
    args = ap.parse_args()
    boot, cfg = _load(args.config)
    _warn_unadapted(cfg)
    deps = _deps(cfg, args.dry_run)
    migrate.refuse_legacy_task_files(cfg.state_dir, args)
    _run_mode(args, boot, cfg, deps)


def _run_mode(args: argparse.Namespace, boot: Config, cfg: Config, deps: Deps) -> None:
    if args.digest:
        send_digest(cfg, deps)
    elif args.triage:
        _enqueue_triage(cfg)
    elif args.triage_run:
        triage.guarded_sweep(cfg, deps)
    elif args.migrate_tmux:
        _migrate_tmux(cfg)
    else:
        with pass_lock(cfg.state_dir):
            # The console stores model settings under this lock: read them
            # again inside it, so a pass never runs a block the console has
            # replaced (a removed track would park the tasks it claims).
            guarded_pass(apply_stored_models(boot), deps, args.config, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
