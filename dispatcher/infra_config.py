"""Text edits of agent-ops-infra's config/targets.yaml: the onboarding adds
its project as a block of text, so the comments and layout of the rest of the
file survive (#225)."""
from __future__ import annotations

import re

_PROJECTS = re.compile(r"projects:\s*(#.*)?")


def add_project_text(text: str, block: str) -> str:
    """`text` with `block` (one project: a YAML list item at column 0)
    appended to its block-style `projects:` list and indented as the list's
    items are. Every other byte is unchanged."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines)
                  if _PROJECTS.fullmatch(line.rstrip("\n"))), None)
    if start is None:
        raise ValueError("no block-style projects: list to add the project to")
    last, indent = _list_end(lines, start)
    if not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines.insert(last + 1, "".join(indent + line if line.strip() else line
                                   for line in block.splitlines(keepends=True)))
    return "".join(lines)


def _list_end(lines: list[str], start: int) -> tuple[int, str]:
    """The index of the list's last content line, and its items' indent.
    The list runs until the next column-0 line that is no item, comment or
    blank."""
    last, indent = start, None
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not lines[i][0].isspace() and not stripped.startswith("-"):
            break
        last = i
        if indent is None and stripped.startswith("-"):
            indent = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
    return last, "  " if indent is None else indent
