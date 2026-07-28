from dispatcher.relogin import LoginPrompt, classify_login

LOGIN_TAIL = """\
Browser didn't open? Use the url below to sign in:

https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code&scope=org%3Acreate_api_key

Paste code here if prompted >
"""

# tmux capture-pane hard-wraps long lines at the pane width
WRAPPED_TAIL = """\
Browser didn't open? Use the url below to sign in:

https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-8
8ed-5944d1962f5e&response_type=code&scope=org%3Acreate_api_key

Paste code here if prompted >
"""

WORKING_TAIL = """\
● Running tests…
  ⎿ python -m pytest -q
✳ Simmering… (esc to interrupt)
"""


def test_login_tail_matches_and_extracts_url():
    got = classify_login(LOGIN_TAIL)
    assert isinstance(got, LoginPrompt)
    assert got.url.startswith("https://claude.ai/oauth/authorize?code=true")
    assert "scope=org%3Acreate_api_key" in got.url


def test_wrapped_url_is_reassembled():
    got = classify_login(WRAPPED_TAIL)
    assert got is not None
    assert "88ed-5944d1962f5e" in got.url  # spans the wrap point


def test_paste_prompt_alone_matches_with_empty_url():
    assert classify_login("Paste code here if prompted >") == LoginPrompt(url="")


def test_working_tail_does_not_match():
    assert classify_login(WORKING_TAIL) is None


def test_unrelated_stall_does_not_match():
    assert classify_login("FAILED tests/test_x.py — retrying (attempt 7)…") is None
    assert classify_login("") is None
