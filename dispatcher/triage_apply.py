"""Validate and execute a triage session's decisions. The session is
read-only; this module is the only GitHub write path. Labels are checked
against the pre-fetched inventory and the backlog taxonomy caps (one type
label, two area labels) — an invalid decision is rejected and reported,
never posted. A score (impact, effort, area) is written through the board,
which accepts it only for an issue nobody has scored yet. A malformed entry or a failing `gh` call is likewise recorded
against its own issue and the loop continues, so the result is always an
accurate account of what was written. Closes are never executed, only passed
through as suggestions for the Telegram report."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

from dispatcher.board import BoardError
from dispatcher.models import TRACK_LABEL_PREFIX

TYPE_LABELS = frozenset({"bug", "enhancement", "documentation", "question"})
AREA_LABELS = frozenset({"frontend", "backend", "infra", "ci", "security",
                         "performance", "testing", "dependencies"})
GH_TIMEOUT = 120


class ApplyError(Exception):
    pass


@dataclass(frozen=True)
class ApplyResult:
    labeled: int
    comments: int
    closes: tuple[str, ...]
    rejected: tuple[str, ...]
    # The subset of `rejected` that GitHub refused rather than the session
    # getting wrong (a failing/timing-out `gh` write). `rejected` still carries
    # them, so the report is unchanged; the split exists because only these are
    # worth retrying — run_sweep holds the cursor when they are all there is.
    write_failures: tuple[str, ...] = ()
    scored: int = 0

    def nothing_written(self) -> bool:
        return self.labeled == 0 and self.comments == 0 and self.scored == 0


def _validate(number: int, add: list[str], remove: list[str],
              inventory: frozenset[str]) -> str | None:
    unknown = [l for l in add + remove if l not in inventory]
    if unknown:
        return f"#{number}: unknown label(s) {unknown}"
    if len([l for l in add if l in TYPE_LABELS]) > 1:
        return f"#{number}: more than one type label in {add}"
    if len([l for l in add if l in AREA_LABELS]) > 2:
        return f"#{number}: more than two area labels in {add}"
    if len([l for l in add if l.startswith(TRACK_LABEL_PREFIX)]) > 1:
        return f"#{number}: more than one track label in {add}"
    return None


def _gh(run, number: int | str, args: list[str]) -> None:
    out = run(["gh"] + args, capture_output=True, text=True,
              timeout=GH_TIMEOUT)
    if out.returncode != 0:
        raise ApplyError(f"#{number}: gh {' '.join(args[:2])} failed: "
                         f"{(out.stderr or '').strip()}")


def _close_line(number: int, close: dict) -> str:
    reason = str(close.get("reason", "")).strip()
    if close.get("kind") == "duplicate":
        return (f"close #{number} as duplicate of "
                f"#{close.get('duplicate_of', '?')} — {reason}")
    return f"close #{number} as not planned — {reason}"


SCORE_KEYS = ("impact", "effort", "area")


@dataclass
class _Tally:
    """What has landed so far. Bumped as each write lands, so a failure
    part-way through an issue keeps whatever already succeeded."""
    labeled: int = 0
    comments: int = 0
    scored: int = 0
    closes: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    write_failures: list[str] = field(default_factory=list)


def _names(issue: dict, key: str) -> list[str]:
    return [str(l) for l in issue.get(key) or []]


def _apply_labels(run, repo: str, number: int, issue: dict,
                  inventory: frozenset[str], tally: _Tally) -> None:
    add, remove = _names(issue, "add_labels"), _names(issue, "remove_labels")
    if not (add or remove):
        return
    problem = _validate(number, add, remove, inventory)
    if problem is not None:
        tally.rejected.append(problem)
        return
    args = ["issue", "edit", str(number), "--repo", repo]
    for l in add:
        args += ["--add-label", l]
    for l in remove:
        args += ["--remove-label", l]
    _gh(run, number, args)
    tally.labeled += 1


def _apply_score(number: int, issue: dict, board, tally: _Tally) -> None:
    if all(issue.get(k) is None for k in SCORE_KEYS):
        return
    try:
        if board is None:
            raise ValueError(f"#{number}: this repo has no board to score on")
        board.score(number, *(issue.get(k) for k in SCORE_KEYS))
        tally.scored += 1
    except ValueError as e:
        tally.rejected.append(str(e))  # already names the issue
    except BoardError as e:
        raise ApplyError(f"#{number}: {e}") from e


def _apply_issue(run, repo: str, number: int, issue: dict,
                 inventory: frozenset[str], board, tally: _Tally) -> None:
    _apply_labels(run, repo, number, issue, inventory, tally)
    _apply_score(number, issue, board, tally)
    comment = str(issue.get("comment") or "").strip()
    if comment:
        _gh(run, number, ["issue", "comment", str(number),
                          "--repo", repo, "--body", comment])
        tally.comments += 1
    close = issue.get("close")
    if close:
        # Free-form agent JSON: a bare string here would reach
        # _close_line's .get() and raise AttributeError past every
        # handler in apply(), aborting the repo the isolation exists to
        # protect. AttributeError is caught there too, as a backstop.
        if not isinstance(close, dict):
            raise TypeError(f"close must be an object, got "
                            f"{type(close).__name__}")
        tally.closes.append(_close_line(number, close))


def apply(repo: str, decisions: dict, inventory: frozenset[str],
          run=subprocess.run, board=None) -> ApplyResult:
    tally = _Tally()
    for issue in decisions.get("issues", []):
        # One issue's malformed entry or failed `gh` call must not abort the
        # repo: the result is the record of what was actually written, and
        # aborting mid-loop would leave earlier writes done but unreported.
        number: int | str = "?"
        try:
            number = int(issue["number"])
            _apply_issue(run, repo, number, issue, inventory, board, tally)
        except ApplyError as e:
            tally.rejected.append(str(e))  # already names the issue
            tally.write_failures.append(str(e))
        except subprocess.SubprocessError as e:
            problem = (f"#{number}: gh call failed "
                       f"({type(e).__name__}: {e})")
            tally.rejected.append(problem)
            tally.write_failures.append(problem)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            tally.rejected.append(f"#{number}: malformed decision entry "
                                  f"({type(e).__name__}: {e})")
    return ApplyResult(labeled=tally.labeled, comments=tally.comments,
                       closes=tuple(tally.closes),
                       rejected=tuple(tally.rejected),
                       write_failures=tuple(tally.write_failures),
                       scored=tally.scored)
