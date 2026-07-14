"""Message templates. Every task message carries issue URL, tmux session
name, and an attach hint."""
from __future__ import annotations

_ATTACH = "attach: `mosh agent-vps -- tmux attach -t task-{issue}`"

_TEMPLATES = {
    "awaiting_spec_review": "📝 #{issue} {title} — spec ready for review.\n{url}\nsession task-{issue} · {note}\n" + _ATTACH,
    "stage_blocked": "🚧 #{issue} {title} — stage blocked: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "pr_opened": "✅ #{issue} {title} — PR opened: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "artifact_failed": "❌ #{issue} {title} — artifact sanity check failed: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "session_crashed": "💀 #{issue} {title} — session died mid-stage. Worktree preserved for autopsy.\n{url}\nsession task-{issue}\n" + _ATTACH,
    "budget_stall": "⏳ #{issue} {title} — usage window exhausted; stalled until reset. {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "budget_resume": "▶️ #{issue} {title} — usage window reset; resuming. {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "implement_started": "🛠 #{issue} {title} — implement started. Plan: {note}\n{url}\nsession task-{issue}\n" + _ATTACH,
    "waiting": "⌛ #{issue} — session stopped and is waiting for input.\nsession task-{issue}\n" + _ATTACH,
}


def render(template: str, **ctx) -> str:
    if template == "daily_digest":
        return "📋 agent-ops daily digest\n" + "\n".join(ctx["lines"])
    return _TEMPLATES[template].format(**ctx)
