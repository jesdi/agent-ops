import pytest

from dispatcher.relogin import LoginPrompt, classify_login

# Full OAuth URL expected in all tests
OAUTH_URL = "https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code&scope=org%3Acreate_api_key"

LOGIN_TAIL = f"""\
Browser didn't open? Use the url below to sign in:

{OAUTH_URL}

Paste code here if prompted >
"""

# tmux capture-pane hard-wraps long lines at the pane width
WRAPPED_TAIL = """\
Browser didn't open? Use the url below to sign in:

https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-8
8ed-5944d1962f5e&response_type=code&scope=org%3Acreate_api_key

Paste code here if prompted >
"""

# URL line immediately followed by Paste line with no blank line between
TIGHT_TAIL = f"""{OAUTH_URL}
Paste code here if prompted >
"""

def hard_wrap(text: str, width: int) -> str:
    """What tmux capture-pane does to a pane of this width: every line is
    chopped into `width`-char rows, blanks preserved."""
    rows: list[str] = []
    for line in text.split("\n"):
        if not line:
            rows.append("")
            continue
        while line:
            rows.append(line[:width])
            line = line[width:]
    return "\n".join(rows)


WORKING_TAIL = """\
● Running tests…
  ⎿ python -m pytest -q
✳ Simmering… (esc to interrupt)
"""


def test_login_tail_matches_and_extracts_url():
    """URL with blank lines before and after should extract cleanly."""
    got = classify_login(LOGIN_TAIL)
    assert isinstance(got, LoginPrompt)
    assert got.url == OAUTH_URL


def test_wrapped_url_is_reassembled():
    """Hard-wrapped URL (split at pane width) should be reassembled."""
    got = classify_login(WRAPPED_TAIL)
    assert got is not None
    assert got.url == OAUTH_URL


def test_tight_tail_url_not_contaminated():
    """URL immediately followed by Paste line (no blank line) should not
    include the Paste text as part of the URL."""
    got = classify_login(TIGHT_TAIL)
    assert got is not None
    assert got.url == OAUTH_URL


@pytest.mark.parametrize("width", [50, 60, 72, 80, 100, 120])
def test_wrapped_url_is_reassembled_at_any_pane_width(width):
    """The URL survives 2, 3 and 4 segment wraps, whatever character the
    wrap happens to land on (uppercase, '-', '_', '&', '=', '?', '.')."""
    got = classify_login(hard_wrap(LOGIN_TAIL, width))
    assert got is not None
    assert got.url == OAUTH_URL


@pytest.mark.parametrize("width", [50, 60, 72, 80])
def test_wrapped_tight_tail_is_not_contaminated_by_prose(width):
    """Even wrapped, the paste prompt must never be glued onto the URL."""
    got = classify_login(hard_wrap(TIGHT_TAIL, width))
    assert got is not None
    assert got.url == OAUTH_URL


def test_paste_prompt_alone_matches_with_empty_url():
    assert classify_login("Paste code here if prompted >") == LoginPrompt(url="")


def test_working_tail_does_not_match():
    assert classify_login(WORKING_TAIL) is None


def test_unrelated_stall_does_not_match():
    assert classify_login("FAILED tests/test_x.py — retrying (attempt 7)…") is None
    assert classify_login("") is None
