# Park on herdr liveness; poke a stopped session instead of parking it — Design

## Decisions

- **Liveness comes from herdr, not from turn-end events.** The dispatcher asks herdr for the
  agent status on every pass and `working` reads as idle zero. Probes: on the mac (herdr 0.9.1)
  Claude Code 2.1.296 and Codex 0.160.1 both reported `working` throughout a 150 second
  foreground tool call and `done` after it, counter unchanged during the call. On the box, Codex
  0.156.1 inside the session container (tab created with `HERDR_AGENT=codex`, as `spawn_stage`
  does) reported `working` throughout a 60 second foreground command and `done` after it. Turn-end
  markers stay as hints; they never park on their own.
- **The waiting marker now means "poke is safe", not "park".** `_working_actions` returns a
  `Poke` action instead of `ParkForInput` for a live session with a waiting marker and an agent
  status other than `working`, and `_stalled` only fires without a waiting marker. A session that
  ended a turn has a composer waiting for text; a session that never did is in a prompt or hung,
  and text would be typed into the wrong place. waitd, the Stop hook, `mark_waiting` and
  `has_waiting` are untouched: a waiting ping still deletes a background marker, so a Claude
  session whose background work finished and then stopped is poked instead of being held for the
  cap.
- **The poke is `Sessions.send_text`, the resume path's own call.** One line, then Enter, no
  new herdr primitive. On the box Codex took the line as a prompt, read `bg-done.txt` written by
  a shell it had backgrounded and forgotten, and continued. Text:
  `Check your background terminals. If the work finished, continue the stage. If it is still
  running, wait for it. If you need the operator, set the stage status to blocked with a note.`
  The last sentence routes a real need for input through the stage signal, which already parks on
  the next pass, so a session that needs the operator costs one poke, not the cap.
- **Cadence is the pass interval, moved to 5 minutes; no new setting.** Every pass that finds a
  stopped session pokes it. A pass that finds it `working` (still answering the previous poke)
  sends nothing. The operator asked for about five minutes between pokes for both runtimes, and
  the pass is the only clock the dispatcher has, so `pass_interval_minutes` defaults to 5 and the
  `OnUnitActiveSec` of `agent-ops-dispatcher.timer` in agent-ops-infra moves with it (the two are
  documented as a pair). No idle threshold guards the poke: a session that stopped seconds before
  the pass is one that ended its turn mid-stage, and "continue" is the right message for it. The
  cost of a misread (herdr `unknown` on a quiet Codex turn) is one queued follow-up line.
- **The cap is `background_wait_seconds`; no new setting.** `TaskState` gains `poked_since`
  (epoch seconds of the first poke, 0.0 when none) and `pokes` (count). A pass that finds the
  session stopped with `now - poked_since > cap` parks for input with
  `(poked N times over 180m — stopped mid-stage without a stage signal)` and ends the session. A
  pass that finds herdr `working` clears both fields: real work resets the clock, a short poke
  answer does not, because by the next pass it is idle again. Session end and replacement clear
  them through the existing reset of per-session task fields.
  <!-- ponytail: a session that does a long turn every poke never caps; the loop caps and stage
  signals bound it. If that bites, cap on pokes without a stage.json mtime change. -->
- **One stall timeout.** `codex_stall_after_seconds` goes. The 1800 second allowance existed
  because herdr read Codex poorly; the box probe shows it reads `working` inside the container,
  and a misread now costs a poke, not a park. `_stall_after` becomes `cfg.stall_after_seconds`.
  The loader warns on the old key and drops it so the box's `targets.yaml` keeps loading.
- **herdr's `unknown` for Codex is tolerated.** The codex manifest falls back to `unknown` when
  no spinner or timer is visible. With a waiting marker that turns into a poke after the timeout,
  harmless; without one it is a stall park as today, and the probe saw `working` for the whole
  tool call, so the exposure is a quiet thinking phase longer than the timeout.
- **The background wait is kept as shipped and takes precedence.** `_in_wait` and
  `_wait_actions` are unchanged. A Claude session with a background marker is neither poked nor
  stalled; Claude Code wakes it itself. Poking it would start a turn on top of the pending wake.
- **Claude sessions are poked too.** One rule for both runtimes. For Claude the poke replaces
  today's immediate park on a turn end with nothing running; a Claude session that finished a
  stage writes `done`, and one that needs input writes `blocked` or `awaiting-answers`, so a
  Claude session that gets poked is one that stopped mid-stage without saying why.
- **`HERDR_AGENT` on the tab is what makes herdr see the containerised agent.** Without it herdr
  finds `podman` as the pane process and reports `agent_not_found`, which `agent_state` maps to
  `("none", 0)` and `idle_seconds` counts as idle forever. `spawn_stage` already sets it. A
  launch path that does not is a stall park waiting to happen; noted, not changed here.

## Data model

| Table | Field | Constraint | Why |
|---|---|---|---|
| herdr `agent get` | `agent_status` | `working` → `idle_seconds` 0; anything else accumulates from the last change (sidecar `herdr-status/<session>.json`) | the only liveness input |
| `waiting-<target>-<issue>` (existing) | presence | still written and cleared by waitd as today | poke is safe; never a park trigger |
| `background-<target>-<issue>` (existing) | unchanged | | background wait, unchanged |
| `TaskState` | `poked_since` (float, default 0.0) | epoch seconds of the first poke of this run of pokes; 0.0 when none | the cap clock |
| | `pokes` (int, default 0) | count since `poked_since` | the park note |
| `Config` | `stall_after_seconds` | unchanged; `codex_stall_after_seconds` removed, warned and ignored when present | one hang timeout for both runtimes |
| `Config` | `pass_interval_minutes` | default 5 (was 10); the systemd timer matches | the poke cadence |
| `Config` | `background_wait_seconds` | unchanged | the poke cap |

Mid-stage decision for one live task with a `working` stage signal, in order:

| Condition | Pass does |
|---|---|
| background marker held (per stop-hook-background-work) | hold, or cap park |
| `stall_after` is 0 or `idle_seconds` is None | nothing |
| herdr `working` (`idle_seconds` 0) | clear `poked_since`/`pokes`; nothing else |
| no waiting marker, `idle_seconds` ≤ `stall_after` | nothing |
| no waiting marker, `idle_seconds` > `stall_after` | park for input, stall note |
| waiting marker, `poked_since` set and `now - poked_since` > cap | park for input, poke-cap note |
| waiting marker, otherwise | `Poke`: send text, set `poked_since` if 0.0, `pokes += 1`, log `poked` |

New action `Poke(text)` beside `ParkForInput`, handled in `main.py` by `Sessions.send_text` and
the task-state update above.

## Identities

None.

## Seams

- `dispatcher.machine.next_actions(task, signal, session_alive, waiting=, idle_seconds=,
  stall_after=, now=, poke_cap=)` and `pass_actions(...)` — existing; two new keyword inputs for
  the cap, defaulting so existing callers and tests keep compiling. `Poke` is a new action.
- `dispatcher.main.run_pass(cfg, deps)` — existing; the sessions fake records `send_text` calls
  and drives `idle_seconds` / `agent_state`.
- `dispatcher.sessions.Sessions.send_text(target, issue, text)` and `idle_seconds(target,
  issue)` — existing.
- `dispatcher.config.load_config(path)` — existing; the removed key warns.

## Touched docs

- `CONTEXT.md`: new **Poke** and **Stall** entries; the **Background wait** entry's _Avoid_ note
  points at both.
- `targets.example.yaml`: drop `codex_stall_after_seconds`; the `stall_after_seconds` comment
  says only a hang parks through it and a stopped turn is poked every pass;
  `pass_interval_minutes` example and comment say 5.
- agent-ops-infra `provision/agent-ops-dispatcher.timer`: `OnUnitActiveSec=5min`, and its
  `config/targets.example.yaml` line for the interval. A separate PR in that repo, deployed with
  this one.
- `docs/specs/2026-09-24-codex-runtime-design.md`: the "herdr does not recognise codex" note is
  superseded; herdr does with `HERDR_AGENT=codex` on the tab.
- `specs/stop-hook-background-work/spec.md`: the two "parks as today" scenarios for a turn end
  with nothing running now poke. Noted there, not rewritten.
- Tests to update: `tests/test_machine.py`, `tests/test_background_wait_dispatcher_acceptance.py`,
  `tests/test_stop_hook_session_acceptance.py`, `tests/test_stop_hook_session_regressions.py`,
  `tests/test_telegram.py` where they assert the immediate waiting park, and the config tests for
  `codex_stall_after_seconds`.
