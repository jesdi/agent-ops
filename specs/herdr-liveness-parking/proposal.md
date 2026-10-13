# Park on herdr liveness; poke a stopped session instead of parking it

## Why

Long review stages get parked while they are still working. On 2026-10-11 portfolio_eval #314
ran `review-diff` (gap check, Codex review, correctness review) as a foreground tool call that
takes many minutes, and the dispatcher parked the task about ten minutes in. Codex-runtime tasks
park the same way even though their stall timeout is 30 minutes. The workaround is to tell the
agent to keep a progress file so a resume can pick up where it left off, which burns tokens on
every park.

The park does not come from the stall timer. It comes from the **waiting marker**: every turn-end
ping writes one, and a live session with a `working` stage signal and a waiting marker is parked
on the next dispatcher pass, which is about ten minutes later. Three things write a marker that
does not mean "this session stopped and needs the operator":

- Codex `notify` fires for threads other than the main turn (a title-generation thread was
  reproduced on 0.156.1), while the main turn keeps running.
- A Claude marker from turn A survives into turn B; a new turn never clears it.
- A turn can end normally while a shell it started keeps running. Claude Code reports those
  shells and wakes itself when they finish; Codex does neither.

The previous attempt (PR #160) fixed this by tracking every turn and shell per runtime and was
rejected as too complex. This proposal uses two things the box already has:

- **herdr's agent status as the liveness source.** `working` already means "never idle". Probes
  on 2026-10-12 and 2026-10-13 (herdr 0.9.1; Claude Code 2.1.296 and Codex 0.160.1 on the mac,
  Codex 0.156.1 inside the session container on the box) showed both runtimes report `working`
  for the whole length of a long foreground tool call, with the state-change counter unchanged,
  and `done` after it. A marker can only be wrong about a session herdr can see is busy.
- **The dispatcher pass as the supervisor.** A session that stopped its turn is not parked; the
  pass types one line into its pane asking it to check its background work and continue. On the
  box, Codex took that line as a prompt, read the result of a shell that had finished while it
  sat idle, and carried on. That is the wake Codex lacks, at the cost of one `send_text` per
  pass, and it needs no inventory of what the session started.

## For whom

The operator, who today lifts these parks by hand and pays for the resumes, and the unattended
stage sessions that lose long reviews or sit on finished background work until someone types.

## Goal

A live session that herdr reports as `working` is never parked mid-stage, whatever markers say.
A live session that ended its turn mid-stage is poked every pass, about every 5 minutes, instead
of parked, until it continues, reports through the stage signal, or has been poked for the
background-wait cap.
A session that is not `working` and never ended a turn (a hang or a login prompt) parks on the
stall timer as today. Both runtimes get the same rule and the same timeout.

## Non-goals

- No change to the Stop hook, Codex `notify`, or waitd. Markers are still written and still
  clear each other; only what the dispatcher does with the waiting marker changes.
- No change to the background wait (Claude turn end with `background_tasks`): it still holds a
  stopped session without poking it, for up to `background_wait_seconds`.
- No background-work inventory for either runtime. The poke asks the agent; the box does not
  look.
- No new poke setting. The cadence is the dispatcher pass, which moves from 10 to 5 minutes for
  this, and the cap is the background-wait cap, already configured.
- No change to parks that come from the stage signal (`blocked`, `awaiting-answers`), from the
  gate, from loop caps, or from a dead session.
- No fix to herdr's status detection. One misread was observed (the Codex hooks-trust dialog
  reads `done` instead of `blocked`); it is harmless here because any status but `working`
  accumulates idle time.
