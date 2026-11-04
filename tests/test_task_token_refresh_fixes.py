"""Ticket 05 beyond the acceptance file: a token whose expiry is unknown is re-minted,
one failing token write leaves the rest of the pass running, and an expiry
record goes with its token directory."""
import re

import pytest

from dispatcher import task_tokens
from dispatcher.state import task_key
from tests.test_task_container_tokens_acceptance import home  # noqa: F401  (fixture)
from tests.test_task_token_refresh_acceptance import Box


@pytest.fixture
def box(tmp_path, monkeypatch, home):  # noqa: F811
    return Box(tmp_path, monkeypatch)


def expiry_record(box, repo):
    return box.tokens.parent / task_tokens.EXPIRY / task_key(repo, 7)


def test_a_token_with_no_recorded_expiry_is_re_minted_on_the_next_pass(box):
    box.spawn_all()
    old = box.hosts("analytics")
    minted = len(box.app.task_mints())
    expiry_record(box, "analytics").unlink()                     # tokens last 60 minutes: not due
    box.run()
    assert box.app.task_mints()[minted:] == [(["jesdi/analytics"], box.app.task_mints()[-1][1])]
    assert box.hosts("analytics") != old
    assert re.findall(r"ghs_host_\d+", box.hosts("analytics")) == [box.app.task_mints()[-1][1]]
    assert expiry_record(box, "analytics").exists()


def test_a_token_write_that_fails_leaves_the_file_and_the_rest_of_the_pass_runs(box, monkeypatch, capsys):
    box.spawn_all()
    old = box.hosts("analytics")
    for repo in ("analytics", "portfolio_eval"):
        expiry_record(box, repo).unlink()                        # both due
    write = task_tokens.write

    def failing(directory, token):
        if directory.name == task_key("analytics", 7):
            raise OSError(28, "No space left on device")
        write(directory, token)

    monkeypatch.setattr(task_tokens, "write", failing)
    portfolio = box.hosts("portfolio_eval")
    box.run()                                                    # does not raise
    assert box.hosts("analytics") == old
    assert box.hosts("portfolio_eval") != portfolio              # the other refresh still ran
    assert "No space left on device" in capsys.readouterr().err


def test_an_expiry_record_goes_with_its_token_directory(box):
    box.spawn_all()
    orphan = box.tokens.parent / task_tokens.EXPIRY / task_key("analytics", 99)
    orphan.write_text("2099-01-01T00:00:00+00:00")
    box.run()
    assert not orphan.exists()
    assert expiry_record(box, "analytics").exists()
