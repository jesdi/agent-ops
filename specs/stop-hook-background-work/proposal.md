# Stop hook: don't park a session that is waiting on background work

## Why

A stage session that ends its turn while background work is still running gets parked and
killed, and nothing brings it back. On 2026-10-01 portfolio_eval #329 (review stage) started
`make crap-gate` as a background shell and ended its turn, expecting to be woken when the gate
finished. The Stop hook treats every turn end as "waiting for input", so the dispatcher parked
the task and ended the session, which killed the gate. The operator's reply resumed it; the new
session backgrounded the gate again and was parked 49 seconds later. A park of this kind asks
the operator nothing, yet only the operator can lift it, and lifting it repeats the loop.

Running gates and other long commands in the background is a cheap way for a session to get
through a big task, so the box should allow it rather than forbid it.

## For whom

The operator of the box, who today has to notice these parks and unblock them by hand, and the
unattended stage sessions (spec, plan, implement, review, address-review) that lose their work.

## Goal

A Claude stage session may end a turn while background work is running and stay alive, for up to
3 hours, until that work finishes and the session carries on with the stage. Every other turn end
parks exactly as it does today.

## Non-goals

- No change to a turn end with no background work: it parks immediately, as today. The session is
  not told to keep going, and an attached operator's conversation gets no special treatment.
- No new stage-signal status. The session does not declare that it is waiting on background work;
  Claude Code reports it to the Stop hook.
- No dispatcher process that auto-resumes parked tasks.
- No change for Codex sessions: Codex's `notify` program reports no background work. They keep
  today's behaviour.
- No detection of background work by inspecting processes or the screen.
- No change to how the 10-minute stall timer treats a session that is not in a background wait.
- No prompt changes.
- No cleanup of the stale legacy `waiting-<issue>` markers in the box's state dir.
