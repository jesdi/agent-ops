"""Classify a captured tmux pane tail as the interactive `claude /login`
prompt. The regex set is deliberately small and documented: the terminal
login flow prints (a) the claude.ai OAuth authorize URL and (b) a
"Paste code here if prompted" input line. Anything else is not a login
stall."""
from __future__ import annotations

import re
from dataclasses import dataclass

# The authorize URL as printed by `claude /login`. When a line is hard-wrapped
# by tmux (fills the pane width), we rejoin it with the next line if that line
# starts with a URL-parameter character (digit, lowercase, or %). This avoids
# false joins with prose text like "Paste code here" that starts with uppercase.
_OAUTH_URL_RE = re.compile(r"https://claude\.ai/oauth/authorize[^\s\"'<>]*")
_PASTE_RE = re.compile(r"paste code here", re.IGNORECASE)


@dataclass(frozen=True)
class LoginPrompt:
    url: str  # "" when the prompt matched but no URL was recoverable


def _rejoin_wrapped_lines(tail: str) -> str:
    """Rejoin hard-wrapped lines caused by tmux pane width limits.

    A line is hard-wrapped if its length equals the maximum line length
    (approximating the pane width). We rejoin it with the next line only if
    the next line starts with a URL-parameter character (digit, lowercase
    letter, or %), not prose text (uppercase letter). This prevents false
    joins with following paragraphs like "Paste code here if prompted >".
    """
    lines = tail.split("\n")
    if len(lines) <= 1:
        return tail

    # Find the maximum line length (approximates pane width for hard-wrapped lines)
    max_len = max(len(line) for line in lines) if lines else 0

    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # Join this line with the next if it's hard-wrapped and the next line
        # looks like a URL continuation (starts with digit, lowercase, or %)
        while i < len(lines) - 1 and len(line) == max_len and len(lines[i + 1]) > 0:
            next_line = lines[i + 1]
            # URL parameters typically start with a digit, lowercase letter, or %
            # (for percent-encoding). This avoids joining with prose that starts
            # with an uppercase letter (e.g., "Paste code here").
            if next_line[0] in "0123456789abcdefghijklmnopqrstuvwxyz%":
                line += next_line
                i += 1
            else:
                break
        result.append(line)
        i += 1

    return "\n".join(result)


def classify_login(tail: str) -> LoginPrompt | None:
    # Rejoin hard-wrapped lines first, then search for the URL in the
    # reconstructed text. Fall back to searching the raw tail.
    unwrapped = _rejoin_wrapped_lines(tail)
    for text in (unwrapped, tail):
        m = _OAUTH_URL_RE.search(text)
        if m:
            return LoginPrompt(url=m.group(0))
    if _PASTE_RE.search(tail):
        return LoginPrompt(url="")
    return None
