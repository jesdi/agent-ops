"""Which entry a stage launches on: the model policy, the usage gate's say,
the sticky pick, a one-shot operator override (kept or dropped), and the
second model."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from dispatcher import eventlog, execution_overrides, priority
from dispatcher.config import Config, Repository, policy_for, routed_providers, stranded
from dispatcher.deps import Admit, _now
from dispatcher.github import Candidate
from dispatcher.models import (Admitted, Entry, ModelPolicy, Order, override_refusal,
                               parse_entry, second_model, stage_pick, track_from_labels)
from dispatcher.state import NO_SLOT, Stage, TaskState, launch_entries, next_stage
from dispatcher.workspace import append_worktree_file


def _policy(cfg: Config, repository: Repository | None) -> ModelPolicy:
    """repository is None for a task whose repository has left the config — it still
    resolves, against the global policy."""
    return policy_for(cfg, repository)


@dataclass(frozen=True)
class Launch:
    """The stage a spawn site is about to start and the entry it runs on,
    chosen once: the usage gate asks about exactly the model the spawner
    then launches, and the spawner records the entry as the stage's pick.
    `second` is the gated second model (models.second_model), decided with
    the launch and never stored, so every spawn and resume re-asks."""
    stage: Stage
    entry: Entry
    second: Entry | None = None

    @property
    def model(self) -> str:
        return self.entry.model_id


ClaimCandidate = tuple[Repository, Candidate, Launch]


def _launch_for(cfg: Config, repository: Repository | None, task: TaskState,
                stage: Stage, admitted: Admitted,
                order: Order) -> Launch | None:
    """The stage's recorded pick, else the first admitted of the entries the
    launch walks (state.launch_entries). None: its track is not usable, or
    nothing is admitted — wait."""
    pick = stage_pick(task.picks, stage.value)
    if pick:
        return Launch(stage, parse_entry(pick, "pick"))
    entries = launch_entries(task, _policy(cfg, repository), order, stage.value)
    entry = next((e for e in entries if admitted(e.model_id)), None)
    return Launch(stage, entry) if entry else None


def _admitted(admit: Admit, bypass: bool) -> Admitted:
    return (lambda m: True) if bypass else (lambda m: admit(m).admitted)


def _with_second(cfg: Config, repository: Repository | None, launch: Launch,
                 admit: Admit) -> Launch:
    """Grant the second model on the gate itself: a bypass never covers it."""
    second = second_model(_policy(cfg, repository), launch.stage.value,
                          launch.entry, _admitted(admit, False))
    return replace(launch, second=second)


def _execution_override_for(cfg: Config, task: TaskState,
                            stage: Stage) -> execution_overrides.ExecutionOverride | None:
    """Revalidate a saved choice against the stage actually about to launch.

    The web's task snapshot can precede a stage transition. Drop an obsolete
    cross-provider choice, including its bypass, before selecting a launch.
    """
    override, refusal = _saved_override(cfg, task, stage)
    if refusal:
        _drop_override(cfg, task, stage, refusal)
    return override


def _saved_override(cfg: Config, task: TaskState, stage: Stage
                    ) -> tuple[execution_overrides.ExecutionOverride | None, str]:
    """The saved choice as it applies to `stage`, and why it may not (then
    None): read only, nothing dropped."""
    override = execution_overrides.load(cfg.state_dir, task.repository, task.issue)
    if override is None or not override.model:
        return override, ""
    refusal = override_refusal(task.picks, stage.value, override.model)
    return (None, refusal) if refusal else (override, "")


def _drop_override(cfg: Config, task: TaskState, stage: Stage, refusal: str) -> None:
    override = execution_overrides.load(cfg.state_dir, task.repository, task.issue)
    execution_overrides.delete(cfg.state_dir, task.repository, task.issue)
    eventlog.append_event(cfg.state_dir, "execution-override-dropped",
                          repository=task.repository, issue=task.issue,
                          stage=stage.value, model=override.model, detail=refusal)


def _choose_launch(
        cfg: Config, repository: Repository | None, task: TaskState, stage: Stage,
        admit: Admit, order: Order,
        override: execution_overrides.ExecutionOverride | None,
) -> tuple[Launch | None, bool, str]:
    """What a spawn site launches, whether usage is bypassed, and why a
    denied launch never comes. A one-shot operator `override`
    (_execution_override_for; on a wake also what the resume named) names
    the entry outright; otherwise the sticky pick or the track decides. A
    denied launch is (None, bypass, why): nothing mutated, the signal
    persists, retried once headroom returns; `why` (models.stranded) says
    when it never returns, and the caller parks."""
    bypass = bool(override and override.bypass_usage)
    if override is not None and override.model:
        launch = Launch(stage, parse_entry(override.model, "override"))
        return _gated(cfg, repository, launch, admit, bypass), bypass, ""
    launch = _launch_for(cfg, repository, task, stage, _admitted(admit, bypass), order)
    gated = _gated(cfg, repository, launch, admit, bypass)
    return gated, bypass, "" if gated else stranded(cfg, repository, task, stage.value)


def _gated(cfg: Config, repository: Repository | None, launch: Launch | None,
           admit: Admit, bypass: bool) -> Launch | None:
    """`launch` with its second model once usage admits it (or is bypassed), else None."""
    if launch is None or not (bypass or admit(launch.model).admitted):
        return None
    return _with_second(cfg, repository, launch, admit)


def _candidate_launch(cfg: Config, repository: Repository, cand: Candidate,
                      admit: Admit, order: Order) -> tuple[Launch | None, bool]:
    """An unclaimed candidate has no picks: its spec entry comes from the
    track its labels name (else the untracked track)."""
    policy = policy_for(cfg, repository)
    probe = TaskState(issue=cand.number, repository=repository.name, stage=Stage.QUEUED,
                      slot=NO_SLOT, worktree="", branch="", title=cand.title,
                      updated_at="", track=track_from_labels(cand.labels, policy))
    launch, bypass, _ = _choose_launch(cfg, repository, probe, Stage.SPEC, admit, order,
                                       _execution_override_for(cfg, probe, Stage.SPEC))
    return launch, bypass   # no picks and a configured track: nothing strands it


def _consume_execution_choice(cfg: Config, repository: str, issue: int) -> None:
    execution_overrides.delete(cfg.state_dir, repository, issue)


def _display_entry(cfg: Config, repository: Repository | None, task: TaskState) -> str:
    """The entry a task runs or would run next, for status lines: its pick for
    the next launch, else the first of the entries that launch walks
    (state.launch_entries), else ''. No usage reading here, so the order is
    built from an empty one: a fixed mode applies, and in auto every entry
    is unrated, which is the written order."""
    pick = stage_pick(task.picks, next_stage(task))
    if pick:
        return pick
    entries = launch_entries(task, _policy(cfg, repository), priority.order(
        priority.load(cfg.state_dir, routed_providers(cfg)), {},
        datetime.now(timezone.utc), cfg.pace))
    return str(entries[0]) if entries else ""


def _last_launched(worktree: str) -> str:
    """The entry of the worktree's last launch, from the models.log
    breadcrumb; "" when there is none."""
    p = Path(worktree) / ".agent" / "models.log"
    lines = p.read_text().splitlines() if p.exists() else []
    return lines[-1].split()[-1] if lines else ""


def _log_model(worktree: str, stage: Stage, model: str) -> None:
    """Durable per-worktree breadcrumb. stage.json is co-owned — sessions
    overwrite it when they signal — so the log is the record that survives."""
    append_worktree_file(worktree, ".agent", "models.log",
                         f"{_now()} {stage.value} {model}\n")
