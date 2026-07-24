import json
from pathlib import Path

from dispatcher import failures
from dispatcher.failures import FailureReport


def report(**kw):
    base = dict(klass="provisioning", target="portfolio_eval", issue=192,
                title="provisioning failed: Add widget",
                error="Traceback (most recent call last):\n  ...\n"
                      "RuntimeError: no python 3.13",
                log_tail="line1\nline2",
                repro="podman run --rm agent-ops-session provision",
                worktree="/w/task-192")
    base.update(kw)
    return FailureReport(**base)


def test_fingerprint_stable_across_noise():
    a = report(error="run at 12:00:01 pid 111\nRuntimeError: no python 3.13")
    b = report(error="run at 23:59:59 pid 999\nRuntimeError: no python 3.13\n\n")
    assert failures.fingerprint(a) == failures.fingerprint(b)
    assert len(failures.fingerprint(a)) == 12


def test_fingerprint_distinct_per_class_and_issue():
    base = report()
    assert failures.fingerprint(base) != failures.fingerprint(
        report(klass="pass-crash"))
    assert failures.fingerprint(base) != failures.fingerprint(report(issue=7))


def test_issue_body_format():
    body = failures.issue_body(
        report(), "https://github.com/jesdi/portfolio_eval/issues/192",
        "2026-07-24T12:00:00+00:00")
    assert body.startswith("🤖 agent-ops failure report\n"
                           "- class: provisioning\n")
    assert "- task: https://github.com/jesdi/portfolio_eval/issues/192\n" in body
    assert "- when: 2026-07-24T12:00:00+00:00\n" in body
    assert "- worktree: /w/task-192\n" in body
    assert "- repro: `podman run --rm agent-ops-session provision`\n" in body
    assert "## Error\n```\n" in body and "no python 3.13" in body
    assert "## Log tail\n```\nline1\nline2\n```" in body
    assert body.rstrip().endswith("Closing this issue unblocks the task.")


def test_issue_body_none_placeholders():
    body = failures.issue_body(
        report(issue=0, worktree="", log_tail=""), "", "2026-07-24T12:00:00+00:00")
    assert "- task: (none)\n" in body
    assert "- worktree: (none)\n" in body
    assert "## Log tail\n```\n(none)\n```" in body


from dispatcher.config import Config, Target


class FakeGitHub:
    def __init__(self, fail=False):
        self.created = []      # (repo, title, body)
        self.fail = fail
        self.next = 500

    def create_issue(self, repo, title, body):
        if self.fail:
            raise RuntimeError("gh outage")
        self.created.append((repo, title, body))
        self.next += 1
        return self.next


class FakeNotifier:
    def __init__(self):
        self.sent = []         # (template, ctx)

    def send(self, template, **ctx):
        self.sent.append((template, ctx))
        return 1


class FakeDeps:
    def __init__(self, github=None, notifier=None):
        self.github = github or FakeGitHub()
        self.notifier = notifier or FakeNotifier()


def cfg(tmp_path, infra_repo="jesdi/agent-ops"):
    return Config(
        state_dir=str(tmp_path / "state"), capacity=3,
        budget_threshold=0.8, racing_minutes=30, racing_threshold=0.95,
        session_memory="2g", session_cpus="2",
        targets=[Target(
            name="portfolio_eval", repo="jesdi/portfolio_eval",
            clone_path=str(tmp_path / "repo"),
            worktrees_path=str(tmp_path / "repo.worktrees"),
            rank_cmd="rank", setup_cmd="setup",
            verify_cmd="make e2e-slot SLOT={slot}",
            project_number=1, project_owner="jesdi",
            status_field_id="F", status_ready_option_id="R",
            status_in_progress_option_id="I",
        )],
        infra_repo=infra_repo,
    )


def test_provisioning_routes_to_infra_repo(tmp_path):
    c, d = cfg(tmp_path), FakeDeps()
    n = failures.report_failure(c, d, report())
    assert n == 501
    repo, title, body = d.github.created[0]
    assert repo == "jesdi/agent-ops"
    assert title == "[agent-ops] provisioning: provisioning failed: Add widget"
    assert "- class: provisioning" in body
    assert "- task: https://github.com/jesdi/portfolio_eval/issues/192" in body
    tmpl, ctx = d.notifier.sent[0]
    assert tmpl == "task_failed"
    assert ctx["url"] == "https://github.com/jesdi/agent-ops/issues/501"
    assert ctx["note"] == "provisioning"
    assert failures.reported(c.state_dir, report())


def test_session_crash_routes_to_target_repo(tmp_path):
    c, d = cfg(tmp_path), FakeDeps()
    failures.report_failure(c, d, report(klass="session-crash"))
    assert d.github.created[0][0] == "jesdi/portfolio_eval"


def test_pass_crash_routes_to_infra_repo(tmp_path):
    c, d = cfg(tmp_path), FakeDeps()
    failures.report_failure(
        c, d, report(klass="pass-crash", target="", issue=0,
                     title="(dispatcher)", worktree=""))
    repo, title, body = d.github.created[0]
    assert repo == "jesdi/agent-ops"
    assert "- task: (none)" in body


def test_dedupe_second_call_files_and_sends_nothing(tmp_path):
    c, d = cfg(tmp_path), FakeDeps()
    first = failures.report_failure(c, d, report())
    again = failures.report_failure(c, d, report())
    assert again == first == 501
    assert len(d.github.created) == 1
    assert len(d.notifier.sent) == 1


def test_outage_leaves_no_marker_so_next_call_retries(tmp_path):
    c = cfg(tmp_path)
    down = FakeDeps(github=FakeGitHub(fail=True))
    assert failures.report_failure(c, down, report()) == 0
    assert not failures.reported(c.state_dir, report())
    up = FakeDeps()
    assert failures.report_failure(c, up, report()) == 501
    assert failures.reported(c.state_dir, report())


def test_no_infra_repo_degrades_to_ping_only(tmp_path):
    c, d = cfg(tmp_path, infra_repo=""), FakeDeps()
    assert failures.report_failure(c, d, report()) == 0
    assert d.github.created == []
    tmpl, ctx = d.notifier.sent[0]
    assert tmpl == "task_failed" and ctx["url"] == ""
    # still deduped: exactly one ping
    failures.report_failure(c, d, report())
    assert len(d.notifier.sent) == 1


def test_report_failure_never_raises(tmp_path):
    class ExplodingNotifier:
        def send(self, template, **ctx):
            raise RuntimeError("telegram down hard")

    c = cfg(tmp_path)
    d = FakeDeps(notifier=ExplodingNotifier())
    assert failures.report_failure(c, d, report()) == 0


def test_dry_run_files_nothing_writes_nothing(tmp_path, capsys):
    c, d = cfg(tmp_path), FakeDeps()
    assert failures.report_failure(c, d, report(), dry_run=True) == 0
    assert d.github.created == [] and d.notifier.sent == []
    assert not failures.reported(c.state_dir, report())
    assert "[dry-run]" in capsys.readouterr().out


def test_tail_returns_last_lines():
    text = "\n".join(f"l{i}" for i in range(100))
    got = failures.tail(text)
    assert got.splitlines()[0] == "l70" and got.splitlines()[-1] == "l99"


def test_setup_log_tail_reads_worktree_log(tmp_path):
    wt = tmp_path / "task-192"
    (wt / ".agent").mkdir(parents=True)
    (wt / ".agent" / "setup.log").write_text("a\nb\nc\n")
    assert failures.setup_log_tail(str(wt)) == "a\nb\nc"
    assert failures.setup_log_tail(str(tmp_path / "missing")) == ""
