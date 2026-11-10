from tests.test_web_sources import _desc_sources
from web.sources import DESCRIPTION_RETRY_SECONDS


class BlankErrGH:
    def __init__(self):
        self.calls = 0

    def issue_view(self, repo, number):
        self.calls += 1
        raise RuntimeError()


def test_message_less_failure_keeps_error_marker_across_retries(tmp_path):
    gh = BlankErrGH()
    src, clock = _desc_sources(tmp_path, gh)
    first = src.issue_description("jesdi/alpha", 1)
    assert first["error"] != ""
    assert src.issue_description("jesdi/alpha", 1) == first and gh.calls == 1
    clock[0] += DESCRIPTION_RETRY_SECONDS
    again = src.issue_description("jesdi/alpha", 1)
    assert gh.calls == 2 and again["error"] != ""
    clock[0] += DESCRIPTION_RETRY_SECONDS
    src.issue_description("jesdi/alpha", 1)
    assert gh.calls == 3
