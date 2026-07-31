"""Validate and execute a triage session's decisions. The session is
read-only; this module is the only GitHub write path. Labels are checked
against the pre-fetched inventory and the backlog taxonomy caps (one type
label, two area labels) — an invalid decision is rejected and reported,
never posted. A malformed entry or a failing `gh` call is likewise recorded
against its own issue and the loop continues, so the result is always an
accurate account of what was written. Closes are never executed, only passed
through as suggestions for the Telegram report."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

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


def _validate(number: int, add: list[str], remove: list[str],
              inventory: frozenset[str]) -> str | None:
    unknown = [l for l in add + remove if l not in inventory]
    if unknown:
        return f"#{number}: unknown label(s) {unknown}"
    if len([l for l in add if l in TYPE_LABELS]) > 1:
        return f"#{number}: more than one type label in {add}"
    if len([l for l in add if l in AREA_LABELS]) > 2:
        return f"#{number}: more than two area labels in {add}"
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


def apply(repo: str, decisions: dict, inventory: frozenset[str],
          run=subprocess.run) -> ApplyResult:
    labeled = comments = 0
    closes: list[str] = []
    rejected: list[str] = []
    for issue in decisions.get("issues", []):
        # One issue's malformed entry or failed `gh` call must not abort the
        # repo: the result is the record of what was actually written, and
        # aborting mid-loop would leave earlier writes done but unreported.
        # The counters are bumped as each write lands, so a failure part-way
        # through an issue keeps whatever already succeeded.
        number: int | str = "?"
        try:
            number = int(issue["number"])
            add = [str(l) for l in issue.get("add_labels") or []]
            remove = [str(l) for l in issue.get("remove_labels") or []]
            if add or remove:
                problem = _validate(number, add, remove, inventory)
                if problem is not None:
                    rejected.append(problem)
                else:
                    args = ["issue", "edit", str(number), "--repo", repo]
                    for l in add:
                        args += ["--add-label", l]
                    for l in remove:
                        args += ["--remove-label", l]
                    _gh(run, number, args)
                    labeled += 1
            comment = str(issue.get("comment") or "").strip()
            if comment:
                _gh(run, number, ["issue", "comment", str(number),
                                  "--repo", repo, "--body", comment])
                comments += 1
            if issue.get("close"):
                closes.append(_close_line(number, issue["close"]))
        except ApplyError as e:
            rejected.append(str(e))  # already names the issue
        except subprocess.SubprocessError as e:
            rejected.append(f"#{number}: gh call failed "
                            f"({type(e).__name__}: {e})")
        except (KeyError, TypeError, ValueError) as e:
            rejected.append(f"#{number}: malformed decision entry "
                            f"({type(e).__name__}: {e})")
    return ApplyResult(labeled=labeled, comments=comments,
                       closes=tuple(closes), rejected=tuple(rejected))
