# Terminal scroll and resize in the web console

## Problem

The reported symptom: "the terminal scrolls with a mouse wheel but not with the
MacBook trackpad."

The symptom is real; the diagnosis it implies is wrong. Measured live against
the box console on two tasks, both showing the static pane-tail block:

| Task | `<pre>` content | Visible box | Scroll range |
| ---- | --------------- | ----------- | ------------ |
| 184  | 392px           | 320px       | 72px (~4 lines) |
| 162  | 408px           | 320px       | 88px (~5 lines) |

Neither page had an xterm mounted — both were the static snapshot. The page body
does not scroll either (`scrollHeight == clientHeight == 813`).

The cause is that there is almost nothing to scroll. `capture_tail`
(`dispatcher/sessions.py:67`) runs `tmux capture-pane -p` with **no `-S` flag**,
so it captures only the *visible* pane — zero scrollback — then trims to the last
25 lines. `TaskPage.tsx` renders that in a `max-h-80` (320px ≈ 20 lines) block.
Roughly five lines of headroom remain.

That fully explains the input-device asymmetry. One wheel notch (~100px) consumes
the entire 72px range in a single tick and feels responsive. The trackpad's
fine-grained pixel deltas move a few pixels and hit the bottom, feeling broken.
Tasks 184 and 162 differ by 16px of range, which is why one felt fine and the
other did not.

Three distinct defects sit behind the one symptom:

1. **No history exists to scroll.** The tail is a visible-pane snapshot.
2. **Heights are hardcoded.** `max-h-80` on the tail block, `h-96` on the xterm.
3. **The live xterm has its own version of (1).** The box carries no tmux config
   at all, so `mouse on` is off and the default `history-limit` applies. tmux
   runs on the alternate screen, so xterm.js's own 5000-line scrollback never
   fills and the wheel cannot reach pane history even when attached.

This design fixes all three.

## Scope

In scope: deep scrollback for both the static tail and the attached terminal;
operator-adjustable terminal height; the copy-mode safety work that `mouse on`
makes mandatory.

Out of scope: any change to how sessions are spawned, staged, or driven; the
dispatcher's stall-detection and re-login classification behaviour (explicitly
preserved unchanged, see A).

## A. Deep scrollback for the static tail

`capture_tail` has two callers with genuinely different needs.
`dispatcher/main.py` uses it at `:106`, `:324`, `:437` and `:598` for stall and
re-login classification, where "the 25 currently visible lines" is the *correct*
semantics — `classify_login` inspects the present screen, not history. Deepening
`capture_tail` in place would silently change stall detection.

Split the seam instead of widening it:

- `capture_tail(issue, lines=25)` is left exactly as it is. Dispatcher-owned,
  visible pane only.
- Add `capture_history(issue, lines=2000)` → `tmux capture-pane -p -S -<lines>`.
  Console-owned.
- Add `GET /api/task/{issue}/history?lines=N`, served from `capture_history`,
  with `N` clamped to 10000. An unbounded `lines` lets a single request pull the
  whole pane history into memory and over the wire; the clamp is silent, matching
  how the console already degrades rather than erroring on tail failures.

History is fetched **on demand**, not folded into the polled task-detail
payload. A 2000-line tail is roughly 150KB, and the task detail is polled
continuously — paying that on every poll is unacceptable on a phone, which is a
primary consumer of this console. The polled `pane_tail` therefore stays the
25-line at-a-glance snapshot it is today, and the tail block gains real history
when the operator asks for it.

`capture_history` degrades the same way `pane_tail` already does in
`web/sources.py:100` — a tmux failure returns `""` rather than 500ing the view.

## B. Operator-adjustable height

The fixed `h-96` (xterm container, `Terminal.tsx`) and `max-h-80` (tail block,
`TaskPage.tsx`) are replaced by a store-driven inline height on a wrapper
carrying `resize-y` and `overflow-auto`, so the operator drags the bottom edge
natively.

This requires **no backend change**. `web/terminal.py` already applies
`TIOCSWINSZ` from the client's `resize` frames, and every viewer already gets its
own *grouped* tmux session (`new-session -t task-<N> -s view-<token>`), so window
sizing is already per-viewer: one operator's height cannot disturb another
viewer or the dispatcher. `Terminal.tsx`'s existing `ResizeObserver` already
calls `fit()` on container resize, so the drag propagates to cols/rows
unmodified.

`useUiStore` (`frontend/src/store/ui.ts`) is currently in-memory only, so a
height stored there would not survive a reload. Add zustand's `persist`
middleware scoped to the height field alone.

`terminalOpenFor` must **not** be persisted. Attaching writes the
`attached-<N>` marker and the dispatcher declines to drive a task while that
marker exists; a persisted attach would re-establish it on page load and stall
a task. This is the same hazard the field's existing comment warns about.

## C. Attached-terminal scrollback, and the hazard it carries

Add `provision/tmux.conf`, converged onto the box by `update.sh` alongside the
other provision files, setting `mouse on` and `history-limit 50000` (roughly
10MB per pane at typical line lengths — affordable on the 4GB box for the handful
of concurrent sessions it runs, and deep enough that a full stage's output stays
reachable). With
`mouse on`, tmux enables mouse reporting on its outer terminal, so xterm.js
forwards wheel events to tmux and tmux scrolls real pane history.

`history-limit` applies only to panes created after the setting takes effect.
Existing sessions keep their old limit; since each stage spawns a fresh session,
this converges naturally and needs no migration.

### The copy-mode leak

Grouped tmux sessions share panes, and copy-mode is pane state, not client
state. Measured behaviour:

| Test | Result |
| ---- | ------ |
| Viewer enters copy-mode → task session's pane | `pane_in_mode: 1` — leaked |
| `capture-pane -p` while parked in copy-mode | returns the **live** grid — safe |
| `window_activity` while parked | still advances — stall detection safe |
| Kill the view session (browser detach) | pane **stays** in copy-mode |
| Dispatcher `send-keys` to a parked pane | **silently not delivered** |

The two middle rows are the good news: `capture_tail` and `idle_seconds`
(`dispatcher/sessions.py:77`, which reads `#{window_activity}`) both keep working
while a pane is parked in copy-mode, so neither the tail nor stall detection
regresses.

The last two rows are the hazard. Enabling `mouse on` without mitigation means:
the operator scrolls up, closes the tab, and the next reply the dispatcher sends
is swallowed by the parked copy-mode. Replies are delivered via `send-keys`
(`dispatcher/sessions.py:104-105`), so this is a silent, total loss of the reply
channel for that task.

### Mitigation

`tmux copy-mode -q -t task-<N>` clears the mode and restores `send-keys`
delivery; this was verified directly. Apply it in two places:

- `web/terminal.py`'s detach cleanup clears copy-mode on the **task** pane (not
  just the view session it already kills). It belongs with the existing
  `_kill_session` step, guarded like its neighbours so a tmux failure cannot
  break the rest of teardown.
- `sessions.py`'s `send_keys` clears it defensively before writing. The detach
  path can be skipped entirely by a crash or an OOM kill, and a silently lost
  reply is the worst failure mode in this system — it is worth paying one extra
  tmux call per reply to make the reply path self-healing.

### Accepted residual

Two operators watching the same task simultaneously share one scroll position,
because they share the pane. This is inherent to grouped sessions and is not
worth engineering around; it is recorded here so it is not later mistaken for a
bug.

## Contingency (not built)

xterm.js gates wheel→mouse-event forwarding on its fractional-pixel accumulator
reaching a whole line. Trackpad input is expected to work through that path, but
it cannot be verified without a trackpad against the box. If it proves flaky,
the fallback is `attachCustomWheelEventHandler` with an accumulator of our own,
which makes the behaviour deterministic and testable independent of xterm's
heuristics.

This is deliberately not built up front. Verification is a required step of
implementation: confirm trackpad scrolling against a live session on the box
before considering C complete.

## Testing

- `capture_history` builds the right `capture-pane -S` invocation, and returns
  `""` on tmux failure.
- `capture_tail` is unchanged — a regression test asserts it still captures the
  visible pane only and still trims to 25 lines, so the dispatcher's
  classification inputs are provably untouched.
- `GET /api/task/{issue}/history` honours `lines`, and 404s for an unknown task
  consistently with the existing task routes.
- The tail block and the xterm container both render at the persisted height,
  and the persisted store round-trips height **without** persisting
  `terminalOpenFor`.
- Detach clears copy-mode on the task pane; `send_keys` delivers to a pane left
  parked in copy-mode. These are the regression tests for the silent-reply-loss
  hazard and are the most important tests in this change.
- Manual, on the box: trackpad scrolling reaches pane history in an attached
  session, and dragging the terminal edge resizes it and survives a reload.
