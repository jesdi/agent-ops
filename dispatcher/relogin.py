"""Classify a captured pane tail as the interactive `claude /login`
prompt. The regex set is deliberately small and documented: the terminal
login flow prints (a) the claude.ai OAuth authorize URL and (b) a
"Paste code here if prompted" input line. Anything else is not a login
stall."""
from __future__ import annotations

import re
from dataclasses import dataclass

# The authorize URL as printed by `claude /login`. The terminal hard-wraps
# it at the pane width, so the tail is un-wrapped first (see
# _rejoin_wrapped_lines).
_OAUTH_URL_RE = re.compile(r"https://claude\.ai/oauth/authorize[^\s\"'<>]*")
_PASTE_RE = re.compile(r"paste code here", re.IGNORECASE)


@dataclass(frozen=True)
class LoginPrompt:
    url: str  # "" when the prompt matched but no URL was recoverable


def _rejoin_wrapped_lines(tail: str) -> str:
    """Rejoin hard-wrapped lines caused by pane width limits.

    The pane width is inferred as the longest row in the tail — nothing can
    exceed it, and a wrapped URL always reaches it. A row of exactly that
    length continues into the next row, repeatedly, so a URL split across
    three or more segments is rejoined too.

    The only guard against gluing prose onto the URL is that a continuation
    row must be a single unbroken token: it is non-empty and contains no
    whitespace. "Paste code here if prompted >" therefore never joins, while
    a wrap landing on any character at all (uppercase, '-', '_', '&', '=')
    does — the first-character heuristic this replaces silently truncated
    the URL for roughly half of all pane widths.
    """
    lines = tail.split("\n")
    if len(lines) <= 1:
        return tail

    width = max(len(line) for line in lines)
    if width == 0:
        return tail

    result: list[str] = []
    i = 0
    while i < len(lines):
        joined = lines[i]
        while (len(lines[i]) == width and i + 1 < len(lines)
               and lines[i + 1] and not any(c.isspace() for c in lines[i + 1])):
            joined += lines[i + 1]
            i += 1
        result.append(joined)
        i += 1

    return "\n".join(result)


def classify_login(tail: str) -> LoginPrompt | None:
    # Search both the raw tail and the un-wrapped one and keep the LONGEST
    # match: un-wrapping can only ever add characters to a wrapped URL, and
    # taking whichever text yields more is what makes a 3+ segment wrap come
    # back whole instead of silently truncated at the first segment boundary.
    candidates = [m.group(0)
                  for m in (_OAUTH_URL_RE.search(text)
                            for text in (tail, _rejoin_wrapped_lines(tail)))
                  if m]
    if candidates:
        return LoginPrompt(url=max(candidates, key=len))
    if _PASTE_RE.search(tail):
        return LoginPrompt(url="")
    return None
