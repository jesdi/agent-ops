"""The `Base:` line of an issue body, and the record that a claim is waiting on it.

The body is untrusted: a value is used only if it is a plain branch name, and
an invalid value is never echoed into a message or command."""
import json
import re
from pathlib import Path

_LINE = re.compile(r"^\s*Base:[ \t]*(.*?)\s*$", re.MULTILINE)
# What `git check-ref-format --branch` accepts, within letters, digits and
# `/ . _ -`: not HEAD; no `..`, empty part, part starting with `.` or ending
# with `.lock`; no trailing `.` or `/`.
_BRANCH = re.compile(
    r"(?!HEAD$)(?!.*(?:\.\.|//|/\.|\.lock/|\.lock$|[./]$))[A-Za-z0-9_][A-Za-z0-9._/-]*")


def parse_base(body: str) -> str | None:
    """The first `Base:` line's branch; "main" when there is none; None when
    its value is not a valid branch name."""
    m = _LINE.search(body or "")
    if m is None:
        return "main"
    v = m.group(1)
    return v if _BRANCH.fullmatch(v) else None


def _path(state_dir: str, repository: str, issue: int) -> Path:
    return Path(state_dir) / f"base-wait-{repository}-{issue}.json"


def is_recorded(state_dir: str, repository: str, issue: int, key: str) -> bool:
    """True when the operator was already pinged for (repository, issue) waiting on `key`.
    A torn or unreadable record counts as none."""
    try:
        return json.loads(_path(state_dir, repository, issue).read_text()).get("key") == key
    except (OSError, ValueError, AttributeError):
        return False


def record_wait(state_dir: str, repository: str, issue: int, key: str) -> None:
    """Record the ping for `key`; atomic, so a crash never leaves a torn file."""
    p = _path(state_dir, repository, issue)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"key": key}))
    tmp.replace(p)


def clear_wait(state_dir: str, repository: str, issue: int) -> None:
    _path(state_dir, repository, issue).unlink(missing_ok=True)
