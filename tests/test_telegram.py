import pytest
import telegram.notify as notify
from telegram.templates import render


def test_templates_carry_url_session_and_attach_hint():
    # The tmux session name is now task-<target>-<issue> (Task 2's rekey); a
    # legacy-adopted session is renamed to this on first touch, so this is
    # the correct name for the operator-facing text to print.
    for name in ["awaiting_spec_review", "stage_blocked", "pr_opened",
                 "artifact_failed", "session_crashed"]:
        msg = render(name, issue=42, title="Add widget",
                     url="https://github.com/x/y/issues/42", note="n",
                     target="acme")
        assert "https://github.com/x/y/issues/42" in msg
        assert "task-acme-42" in msg
        assert "mosh agent-vps -- tmux attach -t task-acme-42" in msg


def test_budget_templates_do_not_reference_a_fictitious_session():
    # budget_stall/budget_resume are box-wide events (issue=0, no target) —
    # there is no single task's session to attach to, so unlike the
    # per-task templates above these must never grow a session/attach line.
    for name in ["budget_stall", "budget_resume"]:
        msg = render(name, issue=0, title="(all tasks)", url="", note="n")
        assert "task-" not in msg
        assert "attach:" not in msg


def test_daily_digest_lists_tasks():
    msg = render("daily_digest", lines=["#42 spec (awaiting review)", "#7 implement"])
    assert "#42 spec (awaiting review)" in msg and "#7 implement" in msg


def test_send_posts_to_bot_api(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT")
    seen = {}
    def capture_and_return(url, payload):
        seen.update(url=url, payload=payload)
        return {}
    monkeypatch.setattr(notify, "_http_post", capture_and_return)
    notify.Notifier().send("pr_opened", issue=42, title="t", url="u", note="",
                           target="acme")
    assert seen["url"] == "https://api.telegram.org/botTOK/sendMessage"
    assert seen["payload"]["chat_id"] == "CHAT"
    assert "task-acme-42" in seen["payload"]["text"]


def test_missing_env_drops_quietly(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(notify, "_http_post",
                        lambda url, payload: (_ for _ in ()).throw(AssertionError))
    notify.Notifier().send("pr_opened", issue=42, title="t", url="u", note="",
                           target="acme")
    assert "TELEGRAM" in capsys.readouterr().err


def test_dry_run_prints(monkeypatch, capsys):
    monkeypatch.setattr(notify, "_http_post",
                        lambda url, payload: (_ for _ in ()).throw(AssertionError))
    notify.Notifier(dry_run=True).send("pr_opened", issue=42, title="t", url="u",
                                       note="", target="acme")
    assert "[dry-run]" in capsys.readouterr().out


def test_parked_question_mentions_reply_and_attach():
    text = render("parked_question", issue=12, title="T", url="u",
                  note="Which auth flow?")
    assert "Which auth flow?" in text and "Reply to THIS message" in text
    assert "/attach 12" in text


def test_status_renders_lines():
    text = render("status", lines=["#1 x — implement [awaiting-ci run 9]"])
    assert "agent-ops status" in text and "#1 x" in text


def test_task_failed_links_the_filed_issue():
    msg = render("task_failed", issue=192,
                 title="provisioning failed: Add widget",
                 url="https://github.com/jesdi/agent-ops/issues/501",
                 note="provisioning")
    assert msg == ("🔥 #192 provisioning failed: Add widget — provisioning\n"
                   "https://github.com/jesdi/agent-ops/issues/501")


def test_queue_template_joins_lines():
    text = render("queue", lines=["1. [5.00] #2 B", "Blocked: #4"])
    assert text == "📊 agent-ops queue\n1. [5.00] #2 B\nBlocked: #4"


def test_awaiting_spec_review_includes_console_link_when_configured():
    msg = render("awaiting_spec_review", issue=42, title="Add widget",
                 url="https://github.com/x/y/issues/42", note="n",
                 target="acme", console="https://box.tail.ts.net")
    assert "read & approve: https://box.tail.ts.net/task/acme/42" in msg


def test_awaiting_spec_review_unchanged_without_console():
    with_empty = render("awaiting_spec_review", issue=42, title="t",
                        url="u", note="n", target="acme", console="")
    without = render("awaiting_spec_review", issue=42, title="t",
                     url="u", note="n", target="acme")
    assert with_empty == without
    assert "read & approve" not in with_empty


def test_notifier_injects_console_url(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT")
    seen = {}
    monkeypatch.setattr(notify, "_http_post",
                        lambda url, payload: seen.update(payload=payload) or {})
    notify.Notifier(console_url="https://box.tail.ts.net").send(
        "awaiting_spec_review", issue=42, title="t", url="u", note="",
        target="acme")
    assert "read & approve: https://box.tail.ts.net/task/acme/42" in seen["payload"]["text"]


def test_needs_relogin_template():
    text = render("needs_relogin", issue=42, title="t",
                  url="https://github.com/o/r/issues/42",
                  login_url="https://claude.ai/oauth/authorize?x=1",
                  note="(no session output for 10m)", target="acme")
    assert "https://claude.ai/oauth/authorize?x=1" in text
    assert "task-acme-42" in text
    assert "claude-home" in text
    assert "Reply to THIS message" in text


def test_spec_parked_message_explains_the_park_and_the_way_back():
    msg = render("spec_parked", issue=42, title="Add widget",
                 url="https://github.com/x/y/issues/42", note="…pane tail…",
                 target="acme")
    assert "#42" in msg and "https://github.com/x/y/issues/42" in msg
    assert "Reply to THIS message" in msg   # the wake path
    assert "task-acme-42" in msg            # /attach hint carries the session


def test_spec_parked_carries_the_console_deep_link_when_configured():
    msg = render("spec_parked", issue=42, title="t", url="u", note="n",
                 target="acme", console="https://box.ts.net")
    assert "https://box.ts.net/task/acme/42" in msg


@pytest.mark.parametrize("template,frag", [
    ("pr_feedback", "review feedback"),
    ("pr_updated", "feedback addressed"),
    ("task_done", "merged"),
    ("pr_closed", "closed without merge"),
])
def test_pr_lifecycle_templates_render(template, frag):
    text = render(template, issue=7, title="Add widget", url="u", note="n",
                  target="acme")
    assert "#7" in text and "Add widget" in text and frag in text


def test_pr_updated_carries_the_new_style_session_name():
    text = render("pr_updated", issue=7, title="t", url="u", note="n",
                  target="acme")
    assert "task-acme-7" in text


def test_resumed_for_attach_carries_the_new_style_session_name():
    text = render("resumed_for_attach", issue=42, title="t", url="u",
                  target="acme")
    assert "task-acme-42" in text
    assert "mosh agent-vps -- tmux attach -t task-acme-42" in text


def test_triage_report_template():
    from telegram.templates import render
    text = render("triage_report", lines=["o/a: 2 labeled", "o/b: FAILED"])
    assert text == "🧹 agent-ops triage\no/a: 2 labeled\no/b: FAILED"


def test_triage_report_truncated_under_the_telegram_limit():
    """Over 4096 chars Telegram returns 400, Notifier.send swallows the
    OSError, and the whole report — the sweep's only output — vanishes."""
    from telegram.templates import TRIAGE_REPORT_LIMIT, render
    lines = [f"o/a: rejected line {n} " + "x" * 80 for n in range(200)]
    text = render("triage_report", lines=lines, triage_dir="/state/triage")
    assert len(text) <= TRIAGE_REPORT_LIMIT
    body = text.splitlines()
    assert body[1] == lines[0]  # keeps the head of the report
    assert body[-1].startswith("… ")
    assert body[-1].endswith("more line(s) (see /state/triage)")
    dropped = int(body[-1].split()[1])
    assert dropped == len(lines) - (len(body) - 2)


def test_triage_report_names_the_state_dir_when_not_given():
    from telegram.templates import render
    text = render("triage_report", lines=[f"line {n} " + "y" * 90
                                          for n in range(200)])
    assert text.splitlines()[-1].endswith("(see <state_dir>/triage/)")


RECOVERY = "CLAUDE_CONFIG_DIR=$HOME/agent-ops-state/claude-home"


def test_unit_failed_template_names_unit_and_recovery():
    msg = render("unit_failed", unit="agent-ops-keepalive.service",
                 host="box1")
    assert "agent-ops-keepalive.service" in msg
    assert "box1" in msg
    assert RECOVERY in msg
    assert "/login" in msg


def test_auth_dark_template_carries_age_and_recovery():
    msg = render("auth_dark", minutes=42, host="box1")
    assert "42" in msg
    assert RECOVERY in msg
    assert "/login" in msg


def test_alert_main_sends_unit_failed(monkeypatch):
    import telegram.alert as alert
    sent = []

    class FakeNotifier:
        def send(self, template, **ctx):
            sent.append((template, ctx))
            return 1

    monkeypatch.setattr(alert, "Notifier", FakeNotifier)
    monkeypatch.setattr(alert.socket, "gethostname", lambda: "box1")
    assert alert.main(["agent-ops-keepalive.service"]) == 0
    assert sent == [("unit_failed", {"unit": "agent-ops-keepalive.service",
                                     "host": "box1"})]


def test_alert_main_without_args_still_sends(monkeypatch):
    import telegram.alert as alert
    sent = []

    class FakeNotifier:
        def send(self, template, **ctx):
            sent.append(template)
            return 1

    monkeypatch.setattr(alert, "Notifier", FakeNotifier)
    assert alert.main([]) == 0
    assert sent == ["unit_failed"]
