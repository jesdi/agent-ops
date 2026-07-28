"""Classify a captured tmux pane tail as the interactive `claude /login`
prompt. The regex set is deliberately small and documented: the terminal
login flow prints (a) the claude.ai OAuth authorize URL and (b) a
"Paste code here if prompted" input line. Anything else is not a login
stall."""
from __future__ import annotations

import re
from dataclasses import dataclass

# The authorize URL as printed by `claude /login`. Applied to the tail
# with newlines stripped too, because tmux hard-wraps long URLs at the
# pane width.
_OAUTH_URL_RE = re.compile(r"https://claude\.ai/oauth/authorize[^\s\"'<>]*")
_PASTE_RE = re.compile(r"paste code here", re.IGNORECASE)


@dataclass(frozen=True)
class LoginPrompt:
    url: str  # "" when the prompt matched but no URL was recoverable


def classify_login(tail: str) -> LoginPrompt | None:
    # Try the unwrapped variant first (newlines removed) so a URL split
    # across pane lines is reassembled. Then fall back to raw tail.
    for text in (tail.replace("\n", ""), tail):
        m = _OAUTH_URL_RE.search(text)
        if m:
            return LoginPrompt(url=m.group(0))
    if _PASTE_RE.search(tail):
        return LoginPrompt(url="")
    return None
