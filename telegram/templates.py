"""Message templates. Every task message carries issue URL, tmux session
name, and an attach hint."""
from __future__ import annotations

from functools import partial

_ATTACH = "attach: `mosh agent-vps -- tmux attach -t task-{repository}-{issue}`"

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

def _listing(head: str, ctx: dict) -> str:
    return head + "\n" + "\n".join(ctx["lines"])


def _triage(ctx: dict) -> str:
    return _triage_report(list(ctx["lines"]), ctx.get("triage_dir") or "<state_dir>/triage/")


def _exam_link(ctx: dict) -> str:
    """The exam page on the console, when the box has one."""
    return f"\n{ctx['console']}/exam/{ctx['repo']}/{ctx['pr']}" if ctx.get("console") else ""


def _exam_ready(ctx: dict) -> str:
    n, debt = ctx["questions"], " (debt)" if ctx.get("debt") else ""
    return (f"📝 Exam ready{debt}: {ctx['repo']}#{ctx['pr']}, {n} question{'' if n == 1 else 's'}."
            + _exam_link(ctx))


def _exam_result(ctx: dict) -> str:
    icon, verdict = ("✅", "pass") if ctx["passed"] else ("🔴", "fail")
    return (f"{icon} Exam {verdict}: {ctx['repo']}#{ctx['pr']}, {ctx['score']}/{ctx['max']}."
            + _exam_link(ctx))


# name -> the text to format with the context, or a function of the context that builds it
_TEMPLATES = {
    "awaiting_plan_review": "📝 {ref} {title} — spec and plan ready for review.\n{url}\nsession task-{repository}-{issue} · {note}\n" + _ATTACH + "{approve}",
    "stage_blocked": "🚧 {ref} {title} — stage blocked: {note}\n{url}\nsession task-{repository}-{issue}\n" + _ATTACH,
    "pr_opened": "✅ {ref} {title} — PR opened: {note}\n{url}\nsession task-{repository}-{issue}\n" + _ATTACH,
    "artifact_failed": "❌ {ref} {title} — artifact sanity check failed: {note}\n{url}\nsession task-{repository}-{issue}\n" + _ATTACH,
    "plan_retry": "🔁 {ref} {title} — plan signal rejected; resuming the session to fix it: {note}\n{url}\nsession task-{repository}-{issue}\n" + _ATTACH,
    "session_crashed": "💀 {ref} {title} — session died mid-stage. Worktree preserved for autopsy.\n{url}\nsession task-{repository}-{issue}\n" + _ATTACH,
    # Box-wide events (issue=0, no single owning task) — never had a real
    # session to attach to, so unlike every per-task template above these
    # carry no session/attach line at all. They stay on a bare "#{issue}"
    # regardless of multi_repository: there is no single project to name.
    "exam_ready": _exam_ready,
    "exam_result": _exam_result,
    "exam_failed": "❌ Exam work failed: {repo}#{pr}: {reason}",
    "budget_stall": "⏳ #{issue} {title} — usage gate closed; resumes when headroom returns. {note}\n{url}",
    "budget_resume": "▶️ #{issue} {title} — usage gate open again; resuming. {note}\n{url}",
    "implement_started": "🛠 {ref} {title} — implement started, one session for {note}\n{url}\nsession task-{repository}-{issue}\n" + _ATTACH,
    "parked_question": "❓ {ref} {title} — needs your input (parked, slot freed):\n{note}\n{url}\nReply to THIS message to answer, or /attach {issue} to take the keyboard.{approve}",
    "plan_parked": ("🌙 {ref} {title} — spec and plan ready and parked for review. "
                    "Session ended; capacity and slot released.\n{note}\n{url}\n"
                    "Reply to THIS message with review feedback (or `approved` to "
                    "start implement), or /attach {issue}.\n" + _ATTACH + "{approve}"),
    "plan_session_stopped": ("🌙 {ref} {title} — the plan session stopped at the review gate "
                             "and was not started again: {rounds} unattended restarts or "
                             "review rounds are used up. Session ended; capacity and slot "
                             "released.\n{note}\n{url}\n"
                             "Reply to THIS message to continue (your reply starts the "
                             "plan session again), or /attach {issue}.\n" + _ATTACH),
    "needs_relogin": ("🔐 {ref} {title} — Claude Code needs re-login. "
                      "Session task-{repository}-{issue} is parked but still LIVE.\n"
                      "Authorize here:\n{login_url}\n\n{note}\n{url}\n"
                      "Shared claude-home: one re-login likely fixes every "
                      "session on this box.\n"
                      "Reply to THIS message with the authorization code.\n"
                      + _ATTACH),
    "resumed_for_attach": "🎹 {ref} {title} — session resumed and holding for you.\n{url}\n" + _ATTACH,
    "task_failed": "🔥 {ref} {title} — {note}\n{url}",
    "review_started": "🔍 {ref} {title} — every ticket landed; review started. {note}\n{url}\nsession task-{repository}-{issue}\n" + _ATTACH,
    "last_round": "⚠️ {ref} {title} — {note}: last round before this task parks.\n{url}\nsession task-{repository}-{issue}\n" + _ATTACH,
    "pr_attention": "🔴 {ref} {title} — PR needs attention ({note}); queued for rework.\n{url}",
    "pr_feedback": "💬 {ref} {title} — review feedback on the PR; queued for rework.\n{note}\n{url}",
    "pr_updated": "🔁 {ref} {title} — feedback addressed, PR updated: {note}\n{url}\nsession task-{repository}-{issue}\n" + _ATTACH,
    "task_done": "🎉 {ref} {title} — PR merged; task done. {note}\n{url}",
    "pr_closed": "🚫 {ref} {title} — PR closed without merge: {note}\n{url}",
    "unit_failed": ("🚨 {unit} FAILED on {host}. Logs:\n"
                    "ssh agent@{host} journalctl --user -u {unit} -n 50"),
    "keepalive_failed": ("🚨 {unit} FAILED on {host}: the Claude OAuth token "
                         "is dying — re-login now:\n" + _RELOGIN),
    "auth_dark": ("🕳 usage unknowable for {minutes}m — dispatcher is "
                  "failing safe and spawning NOTHING. Claude auth on the "
                  "box is likely dead. Fix:\n" + _RELOGIN),
    "daily_digest": partial(_listing, "📋 agent-ops daily digest"),
    "status": partial(_listing, "📟 agent-ops status"),
    "queue": partial(_listing, "📊 agent-ops queue"),
    "triage_report": _triage,
}


def task_ref(repository: str, issue: int, multi_repository: bool) -> str:
    """How a message names a task: `repository#issue` once the box runs more
    than one project, a bare `#issue` otherwise."""
    return f"{repository}#{issue}" if multi_repository and repository else f"#{issue}"


def _task_fields(ctx: dict, multi_repository: bool) -> dict:
    """What a task message derives from its context: how it names the task, and the
    console's approve line for the templates that carry `{approve}`."""
    if "issue" not in ctx:
        return ctx
    repository, console = ctx.get("repository", ""), ctx.get("console")
    approve = f"\nread & approve: {console}/task/{repository}/{ctx['issue']}" if console else ""
    return {**ctx, "ref": task_ref(repository, ctx["issue"], multi_repository), "approve": approve}


def render(template: str, multi_repository: bool = False, **ctx) -> str:
    entry = _TEMPLATES[template]
    if callable(entry):
        return entry(ctx)
    return entry.format(**_task_fields(ctx, multi_repository))
