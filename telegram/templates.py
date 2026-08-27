"""Message templates. Every task message carries issue URL, tmux session
name, and an attach hint."""
from __future__ import annotations

_ATTACH = "attach: `mosh agent-vps -- tmux attach -t task-{issue}`"

# Telegram's sendMessage hard limit is 4096 characters; over it the API
# returns 400 and Notifier.send swallows the OSError — the whole message
# vanishes. The triage report is the one variable-length message here (a line
# per repo, per close suggestion, per rejection), so it is capped with room to
# spare and says how many lines it dropped and where to read them.
TRIAGE_REPORT_LIMIT = 3900


def _triage_report(lines: list[str], where: str) -> str:
    head = "🧹 agent-ops triage"
    kept: list[str] = []
    used = len(head)
    for idx, line in enumerate(lines):
        marker = (f"… {len(lines) - idx} more line(s) "
                  f"(see {where})")
        if used + 1 + len(line) + 1 + len(marker) > TRIAGE_REPORT_LIMIT:
            kept.append(marker)
            break
        kept.append(line)
        used += 1 + len(line)
    return "\n".join([head] + kept)


_RELOGIN = ("ssh -t agent@{host} 'CLAUDE_CONFIG_DIR=$HOME/agent-ops-state/"
            "claude-home $HOME/.local/bin/claude' then /login")

_TEMPLATES = {
    "awaiting_spec_review": "📝 #{issue} {title} — spec ready for review.\n{url}\nsession task-{issue} · {note}\n" + _ATTACH,
    "stage_blocked": "🚧 #{issue} {title} — stage blocked: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "pr_opened": "✅ #{issue} {title} — PR opened: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "artifact_failed": "❌ #{issue} {title} — artifact sanity check failed: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "plan_retry": "🔁 #{issue} {title} — plan format check failed; resuming the session to fix it: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "session_crashed": "💀 #{issue} {title} — session died mid-stage. Worktree preserved for autopsy.\n{url}\nsession task-{issue}\n" + _ATTACH,
    "budget_stall": "⏳ #{issue} {title} — usage window exhausted; stalled until reset. {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "budget_resume": "▶️ #{issue} {title} — usage window reset; resuming. {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "implement_started": "🛠 #{issue} {title} — implement started. Plan: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "parked_question": "❓ #{issue} {title} — needs your input (parked, slot freed):\n{note}\n{url}\nReply to THIS message to answer, or /attach {issue} to take the keyboard.",
    "spec_parked": ("🌙 #{issue} {title} — spec ready and parked for review. "
                    "Session ended; capacity and slot released.\n{note}\n{url}\n"
                    "Reply to THIS message with review feedback (or `ok` to "
                    "continue to plan), or /attach {issue}.\n" + _ATTACH),
    "needs_relogin": ("🔐 #{issue} {title} — Claude Code needs re-login. "
                      "Session task-{issue} is parked but still LIVE.\n"
                      "Authorize here:\n{login_url}\n\n{note}\n{url}\n"
                      "Shared claude-home: one re-login likely fixes every "
                      "session on this box.\n"
                      "Reply to THIS message with the authorization code.\n"
                      + _ATTACH),
    "resumed_for_attach": "🎹 #{issue} {title} — session resumed and holding for you.\n{url}\n" + _ATTACH,
    "task_failed": "🔥 #{issue} {title} — {note}\n{url}",
    "pr_feedback": "💬 #{issue} {title} — review feedback on the PR; queued for rework.\n{note}\n{url}",
    "pr_updated": "🔁 #{issue} {title} — feedback addressed, PR updated: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "task_done": "🎉 #{issue} {title} — PR merged; task done. {note}\n{url}",
    "pr_closed": "🚫 #{issue} {title} — PR closed without merge: {note}\n{url}",
    "unit_failed": ("🚨 {unit} FAILED on {host}. If this is the keepalive, "
                    "the Claude OAuth token is dying — re-login now:\n"
                    + _RELOGIN),
    "auth_dark": ("🕳 usage unknowable for {minutes}m — dispatcher is "
                  "failing safe and spawning NOTHING. Claude auth on the "
                  "box is likely dead. Fix:\n" + _RELOGIN),
}


def render(template: str, **ctx) -> str:
    if template == "daily_digest":
        return "📋 agent-ops daily digest\n" + "\n".join(ctx["lines"])
    if template == "status":
        return "📟 agent-ops status\n" + "\n".join(ctx["lines"])
    if template == "queue":
        return "📊 agent-ops queue\n" + "\n".join(ctx["lines"])
    if template == "triage_report":
        return _triage_report(list(ctx["lines"]),
                              ctx.get("triage_dir") or "<state_dir>/triage/")
    text = _TEMPLATES[template].format(**ctx)
    if template in ("awaiting_spec_review", "spec_parked") and ctx.get("console"):
        text += (f"\nread & approve: {ctx['console']}/task/"
                 f"{ctx['target']}/{ctx['issue']}")
    return text
