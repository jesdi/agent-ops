"""Claiming new work: board reconcile, the per-pass claim round across
projects, quarantine and re-rank checks, and provisioning + committing a
claim (state saved before the irreversible board mutation)."""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterator

from dispatcher import base_line, claims, eventlog, failures, github_app, host_auth, labels
from dispatcher.board import BoardError
from dispatcher.config import Config, Repository, by_project, policy_for
from dispatcher.deps import Admit, Deps, _now
from dispatcher.executor import _Turn, _apply, _report_provisioning_failure
from dispatcher.github import Candidate
from dispatcher.launch_choice import (ClaimCandidate, Launch, _candidate_launch,
                                      _consume_execution_choice)
from dispatcher.lifecycle import (Event, EventType, Ignore, Ranked, Reject,
                                  SweepTombstone, Transition)
from dispatcher.models import Order, track_from_labels
from dispatcher.stage_launch import _spawn_stage, _task_token
from dispatcher.state import (Stage, TaskState, _rank_rejected, _rank_rejected_path,
                              _readable_tasks, allocate_slot, load, load_all, max_slots,
                              save)
from dispatcher.workspace import MissingBaseError, create_workspace


def _reconcile_boards(cfg: Config, deps: Deps) -> None:
    """Every project's board, paused ones too, gets a card for each new open
    issue and its repositories the label set (#230 D8). A failure skips
    that project until the next pass."""
    tracks = labels.tracks(cfg)
    for name, members in by_project(cfg.repositories).items():
        try:
            synced = deps.github.reconcile_board(members, tracks)
        except Exception as exc:  # this project only: the pass goes on
            print(f"[warn] {name}: board reconcile failed — {exc}", file=sys.stderr)
            continue
        for line in synced.lines():
            print(f"[board] {name}: {line}")
        for error in synced.errors:
            print(f"[warn] {name}: board reconcile — {error}", file=sys.stderr)


def _no_task_tokens(deps: Deps) -> str:
    """Why no task token can be minted this pass, or "": the App is not
    registered (the infra README checklist) or the App errored."""
    try:
        registered = deps.access.app.registered()
    except github_app.AppError as exc:
        return f"the GitHub App is unavailable: {exc}"
    return "" if registered else f"the GitHub App is not registered: {github_app.README}"


def _ranked_claim_candidates(deps: Deps, members: list[Repository]):
    """Read one project's ranked candidates, warning when its turn cannot run."""
    why = _no_task_tokens(deps)
    if why:
        print(f"[warn] {members[0].project.name}: no claims this pass — {why}",
              file=sys.stderr)
        return ()
    try:
        return deps.github.candidates(members)
    except (BoardError, host_auth.HostWait) as exc:
        print(f"[warn] {members[0].project.name}: no claims this pass — {exc}",
              file=sys.stderr)
        return ()


def _quarantined(cfg: Config, deps: Deps, repository: Repository, cand: Candidate,
                 skipped: set[tuple[str, int]]) -> bool:
    """A blocker issue holds the card back; it lands in `skipped`."""
    held = failures.check_quarantine(cfg.state_dir, deps.github, repository.name, cand.number)
    if held:
        skipped.add((repository.name, cand.number))
    return held


def _ranked_again(cfg: Config, deps: Deps, by_issue: dict, repository: Repository,
                  cand: Candidate, pass_started: str) -> bool:
    """Whether a ranked card is claimed fresh: it has no task, or the table
    swept its ended task's file on `candidate-ranked`."""
    task = by_issue.get((repository.name, cand.number))
    if task is None:
        return True
    if _rank_rejected(cfg.state_dir, task):
        return False
    this_pass = bool(pass_started) and task.updated_at >= pass_started
    turn = _Turn(cfg, deps, repository, admit=None, order=None, dry_run=False)
    outcome = _apply(turn, task, Event(EventType.CANDIDATE_RANKED, Ranked(this_pass)))
    if isinstance(outcome, Reject):
        _rank_rejected_path(cfg.state_dir, task.repository, task.issue).write_text(task.updated_at)
    return _swept(outcome)


def _swept(outcome: Transition | Ignore | Reject) -> bool:
    return isinstance(outcome, Transition) and any(
        isinstance(a, SweepTombstone) for a in outcome.actions)


def _claimable(cfg: Config, deps: Deps, members: list[Repository], tasks: list[TaskState],
               admit: Admit, order: Order, pass_started: str,
               skipped: set[tuple[str, int]]) -> Iterator[ClaimCandidate]:
    """One project's ranked candidates this pass may claim, across its member
    repositories in rank order, each with its repository and the spec launch it
    would start. Skips quarantined issues, issues whose task stays (candidate-ranked),
    and candidates whose spec model has no headroom — a denied candidate
    leaves the next one eligible, since its model may differ."""
    by_name = {r.name: r for r in members}
    by_issue = {(t.repository, t.issue): t for t in tasks}
    for cand in _ranked_claim_candidates(deps, members):
        repository = by_name[cand.repository]
        if _quarantined(cfg, deps, repository, cand, skipped):
            continue
        launch = _fresh_claim_launch(cfg, deps, by_issue, repository, cand, admit, order,
                                     pass_started)
        if launch is not None:
            yield repository, cand, launch


def _fresh_claim_launch(cfg: Config, deps: Deps, by_issue: dict, repository: Repository,
                        cand: Candidate, admit: Admit, order: Order,
                        pass_started: str) -> Launch | None:
    """The spec launch a ranked card is claimed fresh with; None when its
    task stays or policy rejects it."""
    if not _ranked_again(cfg, deps, by_issue, repository, cand, pass_started):
        return None
    launch, _bypass = _candidate_launch(cfg, repository, cand, admit, order)
    return launch


class ClaimRoundOutcome(Enum):
    """How claiming one picked candidate ends for the round."""

    CONTINUE = auto()
    STOP = auto()
    PROJECT_FAILED = auto()
    CLAIMED = auto()


@dataclass
class ClaimRound:
    """Mutable state of one claim round."""

    free_slots: int
    projects: dict[str, list[Repository]]
    counts: dict[str, int]
    last_claimed: dict[str, str]
    caps: dict[str, int | None]
    candidates: dict[str, Iterator[ClaimCandidate]]


def _commit_claim(cfg: Config, deps: Deps, repository: Repository, cand: Candidate,
                  slot: int, wt: str, launch: Launch, base: str = "main") -> TaskState:
    """Board mutation is last and irreversible: state is saved and the
    `claimed` event appended first, so a crash before the claim leaves a
    recoverable partial claim rather than a silently lost slot. A failure
    from here on releases the board claim back to Ready. A repository the
    installation doesn't reach is claimed and parked; an App error leaves it
    claimed and waking(launch), for the next pass (_task_token)."""
    task = TaskState(issue=cand.number, repository=repository.name,
                     stage=Stage.QUEUED, slot=slot, worktree=wt,
                     branch=f"agent/task-{cand.number}",
                     title=cand.title, updated_at=_now(),
                     effort=cand.effort, labels=cand.labels, base=base,
                     track=track_from_labels(cand.labels, policy_for(cfg, repository)))
    save(cfg.state_dir, task)  # state exists BEFORE the irreversible claim, so a partial claim is recoverable
    eventlog.append_event(cfg.state_dir, "claimed", repository=repository.name,
                          issue=cand.number, stage=Stage.QUEUED.value)
    try:
        deps.github.claim(repository, cand)  # irreversible board mutation — last
        gh_config = _task_token(cfg, deps, repository, task)
        if gh_config is None:
            return load(cfg.state_dir, repository.name, cand.number) or task
        task = _spawn_stage(cfg, deps, repository, task, launch, gh_config=gh_config)
        _consume_execution_choice(cfg, repository.name, cand.number)
    except Exception:
        deps.github.release(repository, cand.number, "claim/spawn failed after provisioning")
        raise
    return task


def _wait_in_ready(cfg: Config, deps: Deps, repository: Repository, number: int,
                   key: str, why: str) -> None:
    """Leave the issue in Ready; ping the operator only the first time for this key."""
    if base_line.is_recorded(cfg.state_dir, repository.name, number, key):
        return
    # The notifier returns 0 on a failed send: record only a delivered ping, so a
    # failed one is retried on the next pass.
    if deps.notifier.send("status", lines=[f"#{number} waits in Ready: {why}"]):
        base_line.record_wait(cfg.state_dir, repository.name, number, key)


def _claim_picked(cfg: Config, deps: Deps, repository: Repository,
                  cand: Candidate, launch: Launch,
                  dry_run: bool) -> ClaimRoundOutcome:
    """Provision and commit one ranked candidate; return its round outcome."""
    base = _read_claim_base(cfg, deps, repository, cand)
    if base is None:
        return ClaimRoundOutcome.CONTINUE
    slot = allocate_slot(load_all(cfg.state_dir), max_slots(cfg.capacity))
    if slot is None:
        return ClaimRoundOutcome.STOP
    try:
        wt = create_workspace(repository, cand.number, base, dry_run=dry_run,
                              auth=deps.github.auth)
    except MissingBaseError:
        # Not claimed, not a failure: the issue stays Ready and every pass
        # asks again; the operator hears once per issue and base.
        _wait_in_ready(cfg, deps, repository, cand.number,
                       f"base:{base}", f"base branch {base} is not on origin")
        return ClaimRoundOutcome.CONTINUE
    except Exception:
        # Board never claimed (claim is last, still Ready) → no release
        # needed; report + quarantine, and the pass survives. Cap at one
        # provisioning failure per project per pass: a systemic fault (git
        # remote down, podman down, worktrees volume full) would otherwise
        # fail EVERY remaining Ready candidate. The project leaves this
        # pass's round; the next pass retries its remaining candidates.
        _report_provisioning_failure(cfg, deps, repository, cand, dry_run)
        return ClaimRoundOutcome.PROJECT_FAILED
    base_line.clear_wait(cfg.state_dir, repository.name, cand.number)
    _commit_claim(cfg, deps, repository, cand, slot, wt, launch, base)
    return ClaimRoundOutcome.CLAIMED


def _read_claim_base(cfg: Config, deps: Deps, repository: Repository,
                     cand: Candidate) -> str | None:
    """Read and validate a candidate's base line, recording invalid bases."""
    try:
        body = deps.github.issue_body(repository, cand.number)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            OSError, ValueError) as exc:  # ValueError: gh printed malformed JSON
        # Unclaimed and still Ready: the next pass reads it again.
        print(f"[warn] body of #{cand.number} not read: {exc}", file=sys.stderr)
        return None
    base = base_line.parse_base(body)
    if base is None:
        _wait_in_ready(cfg, deps, repository, cand.number, "invalid",
                       "its base line names no valid branch")
        return None
    return base


def _claim_round_turn(cfg: Config, deps: Deps, state: ClaimRound,
                      dry_run: bool) -> bool:
    """Take one project turn; False when the round must stop."""
    name = claims.pick_project(list(state.projects), state.counts,
                               state.last_claimed, state.caps)
    if name is None:
        return False
    picked = next(state.candidates[name], None)
    if picked is None:
        del state.projects[name]
        return True
    repository, cand, launch = picked
    outcome = _claim_picked(cfg, deps, repository, cand, launch, dry_run)
    if outcome is ClaimRoundOutcome.STOP:
        return False
    if outcome is ClaimRoundOutcome.PROJECT_FAILED:
        del state.projects[name]
    if outcome is ClaimRoundOutcome.CLAIMED:
        state.counts[name] += 1
        state.last_claimed[name] = _now()
        state.free_slots -= 1  # checked before the next pull, so no candidate is vetted that cannot be claimed
    return True


def _claim_generators(
        cfg: Config, deps: Deps, in_round: dict[str, list[Repository]],
        all_tasks: list[TaskState], admit: Admit, order: Order,
        pass_started: str, skipped: set[tuple[str, int]],
) -> dict[str, Iterator[ClaimCandidate]]:
    """Lazy per-project candidate streams for the claim round."""
    return {name: _claimable(cfg, deps, members, all_tasks, admit, order,
                             pass_started, skipped)
            for name, members in in_round.items()}


def _claim_room(cfg: Config) -> tuple[list[TaskState], int] | None:
    """Every task and the free claim room; None when nothing may be claimed:
    no room, or a task file cannot be read (_readable_tasks)."""
    all_tasks = _readable_tasks(cfg.state_dir)
    if all_tasks is None:
        return None
    free = claims.claim_room(cfg.capacity, cfg.max_open, all_tasks,
                             triage_running=False)
    return (all_tasks, free) if free > 0 else None


def _claim_round_data(cfg: Config, deps: Deps,
                      repositories: list[Repository], admit: Admit,
                      order: Order, pass_started: str,
                      skipped: set[tuple[str, int]]) -> ClaimRound | None:
    """Build one claim round's state, or None when there is no claim room."""
    room = _claim_room(cfg)
    if room is None:
        return None
    all_tasks, free = room
    in_round = by_project([r for r in repositories if not r.project.paused])
    project_of = {r.name: r.project.name for r in repositories}
    counts = claims.project_counts(list(in_round), all_tasks, project_of)
    last = claims.project_last_claims(claims.last_claims(cfg.state_dir), project_of)
    caps = {name: members[0].project.max_active for name, members in in_round.items()}
    # Generator bodies run on first next(), so a project's board is read only on its turn.
    gens = _claim_generators(cfg, deps, in_round, all_tasks, admit, order,
                             pass_started, skipped)
    return ClaimRound(free_slots=free, projects=in_round, counts=counts,
                      last_claimed=last, caps=caps, candidates=gens)


def _run_claim_round(cfg: Config, deps: Deps, repositories: list[Repository],
                     admit: Admit, order: Order, dry_run: bool,
                     pass_started: str, skipped: set[tuple[str, int]]) -> None:
    """Claim until capacity or candidates run out; quarantine skips land in `skipped`."""
    state = _claim_round_data(cfg, deps, repositories, admit, order,
                              pass_started, skipped)
    while state is not None and state.free_slots > 0:
        if not _claim_round_turn(cfg, deps, state, dry_run):
            return


def _claim_new(cfg: Config, deps: Deps, repositories: list[Repository],
               admit: Admit, order: Order, dry_run: bool,
               pass_started: str = "") -> None:
    """Claim free units one at a time, each via claims.pick_project. A paused
    project claims nothing; a project leaves the round when its candidates run
    out or provisioning fails for it. Nothing is claimed while a task file
    cannot be read: the task it holds may own the issue a claim would take
    again, and a capacity unit and a slot no count can see."""
    skipped: set[tuple[str, int]] = set()
    try:
        _run_claim_round(cfg, deps, repositories, admit, order, dry_run,
                         pass_started, skipped)
    finally:
        failures.write_quarantine_verdict(cfg.state_dir, pass_started, skipped)
