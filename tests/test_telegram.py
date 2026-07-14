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
    monkeypatch.setattr(notify, "_http_post",
                        lambda url, payload: seen.update(url=url, payload=payload))
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
