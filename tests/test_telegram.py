import telegram.notify as notify
from telegram.templates import render


def test_templates_carry_url_session_and_attach_hint():
    for name in ["awaiting_spec_review", "stage_blocked", "pr_opened",
                 "artifact_failed", "session_crashed", "budget_stall",
                 "budget_resume"]:
        msg = render(name, issue=42, title="Add widget",
                     url="https://github.com/x/y/issues/42", note="n")
        assert "https://github.com/x/y/issues/42" in msg
        assert "task-42" in msg
        assert "mosh agent-vps -- tmux attach -t task-42" in msg


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
    notify.Notifier().send("pr_opened", issue=42, title="t", url="u", note="")
    assert seen["url"] == "https://api.telegram.org/botTOK/sendMessage"
    assert seen["payload"]["chat_id"] == "CHAT"
    assert "task-42" in seen["payload"]["text"]


def test_missing_env_drops_quietly(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(notify, "_http_post",
                        lambda url, payload: (_ for _ in ()).throw(AssertionError))
    notify.Notifier().send("pr_opened", issue=42, title="t", url="u", note="")
    assert "TELEGRAM" in capsys.readouterr().err


def test_dry_run_prints(monkeypatch, capsys):
    monkeypatch.setattr(notify, "_http_post",
                        lambda url, payload: (_ for _ in ()).throw(AssertionError))
    notify.Notifier(dry_run=True).send("pr_opened", issue=42, title="t", url="u", note="")
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
                 console="https://box.tail.ts.net")
    assert "read & approve: https://box.tail.ts.net/task/42" in msg


def test_awaiting_spec_review_unchanged_without_console():
    with_empty = render("awaiting_spec_review", issue=42, title="t",
                        url="u", note="n", console="")
    without = render("awaiting_spec_review", issue=42, title="t",
                     url="u", note="n")
    assert with_empty == without
    assert "read & approve" not in with_empty


def test_notifier_injects_console_url(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "TOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT")
    seen = {}
    monkeypatch.setattr(notify, "_http_post",
                        lambda url, payload: seen.update(payload=payload) or {})
    notify.Notifier(console_url="https://box.tail.ts.net").send(
        "awaiting_spec_review", issue=42, title="t", url="u", note="")
    assert "read & approve: https://box.tail.ts.net/task/42" in seen["payload"]["text"]


def test_needs_relogin_template():
    text = render("needs_relogin", issue=42, title="t",
                  url="https://github.com/o/r/issues/42",
                  login_url="https://claude.ai/oauth/authorize?x=1",
                  note="(no session output for 10m)")
    assert "https://claude.ai/oauth/authorize?x=1" in text
    assert "task-42" in text
    assert "claude-home" in text
    assert "Reply to THIS message" in text


def test_spec_parked_message_explains_the_park_and_the_way_back():
    msg = render("spec_parked", issue=42, title="Add widget",
                 url="https://github.com/x/y/issues/42", note="…pane tail…")
    assert "#42" in msg and "https://github.com/x/y/issues/42" in msg
    assert "Reply to THIS message" in msg   # the wake path
    assert "task-42" in msg                 # /attach hint carries the session


def test_spec_parked_carries_the_console_deep_link_when_configured():
    msg = render("spec_parked", issue=42, title="t", url="u", note="n",
                 console="https://box.ts.net")
    assert "https://box.ts.net/task/42" in msg
