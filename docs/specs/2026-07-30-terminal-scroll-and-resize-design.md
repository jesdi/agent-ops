# Terminal scroll and resize in the web console

## Problem

The reported symptom: "the terminal scrolls with a mouse wheel but not with the
MacBook trackpad."

Measured against the box console with the terminal **attached** and the outgoing
WebSocket frames instrumented, on two tasks that behave differently:

|                            | task 162                          | task 184                        |
| -------------------------- | --------------------------------- | ------------------------------- |
| App requested mouse report | **yes** (Claude Code)             | **no**                          |
| 23 wheel events produced   | 4 × `ESC[<64;…M` (SGR wheel-up)   | 4 × `ESC O A` (**arrow-up**)    |
| xterm's own scrollback     | `scrollHeight == clientHeight`    | `scrollHeight == clientHeight`  |

xterm.js picks one of three wheel paths depending on what the application inside
the session happened to request. **None of them reach tmux's pane history**, and
two of them are actively wrong:

1. **Mouse reporting on (162).** The wheel becomes SGR mouse reports, but xterm
   **discards magnitude** — one report per wheel event regardless of delta. A
   120px mouse notch carries ~8 lines of intent and scrolls *one line*; 60px of
   trackpad travel produced one report in total. It does scroll, far too slowly
   to feel like scrolling. This is the "works with a mouse, not with the
   trackpad" case: a mouse emits few large discrete notches, a trackpad emits
   many tiny deltas that xterm's fractional accumulator largely swallows.
2. **Mouse reporting off (184).** xterm falls back to translating the wheel into
   **arrow keys injected into the application**. It does not scroll at all, and
   the injected arrow-ups recall prompt history into Claude Code's composer.
   This is a correctness bug, not a slow-scrolling one.
3. **xterm's own 5000-line scrollback is permanently empty**, because tmux holds
   the alternate screen. It can never be the answer.

tmux `mouse on` is **not** enabled on the box. That is confirmed by (2): if tmux
owned mouse reporting, it would be on for every session and 184 would have
emitted SGR reports too.

Two further defects, found while measuring:

- **No history exists to scroll in the unattached view.** `capture_tail`
  (`dispatcher/sessions.py:67`) runs `tmux capture-pane -p` with **no `-S`**, so
  it captures only the visible pane, then trims to 25 lines. Rendered in a
  `max-h-80` block that is ~20 lines tall, this leaves ~5 lines of headroom
  (measured: 392/320 on 184, 408/320 on 162).
- **Heights are hardcoded**: `h-96` on the xterm container, `max-h-80` on the
  tail block.

## Approach

The root problem is that scroll behaviour is *delegated* — it depends on what the
inner application requested, so it is neither predictable nor testable. The fix
is to stop delegating: the console takes ownership of the wheel and scrolls
something it controls.

A live TUI genuinely has no scrollback; only tmux's pane history does. So the
live terminal stays strictly live, and history becomes an explicit, separately
rendered view fed by `capture-pane -S`. Scrolling up over the live terminal
enters that view.

This deliberately avoids tmux copy-mode — see *Rejected alternative* below.

## A. A history endpoint

`capture_tail` has two callers with different needs. `dispatcher/main.py` uses it
at `:106`, `:324`, `:437` and `:598` for stall and re-login classification, where
"the 25 currently visible lines" is the *correct* semantics — `classify_login`
inspects the present screen, not history. Deepening `capture_tail` in place would
silently change stall detection.

Split the seam instead of widening it:

- `capture_tail(issue, lines=25)` is left exactly as it is. Dispatcher-owned,
  visible pane only.
- Add `capture_history(issue, lines=2000)` → `tmux capture-pane -p -S -<lines>`.
  Console-owned.
- Add `GET /api/task/{issue}/history?lines=N`, served from `capture_history`,
  with `N` clamped to 10000. Unbounded `lines` would let one request pull an
  entire pane history into memory and over the wire.

History is fetched **on demand**, never folded into the polled task-detail
payload: a 2000-line tail is roughly 150KB, and the detail endpoint is polled
continuously, which is unacceptable on a phone. The polled `pane_tail` stays the
25-line at-a-glance snapshot it already is.

`capture_history` degrades exactly as `pane_tail` does today
(`web/sources.py:100`) — a tmux failure returns `""` rather than 500ing the view.

## B. Owning the wheel, and the history view

In `Terminal.tsx`, register `attachCustomWheelEventHandler` (confirmed present in
the shipped bundle) returning `false` so xterm never applies **either** default:
no SGR reports, no injected arrow keys. Task 184's keystroke injection is fixed
by this alone.

The handler accumulates pixel deltas and, on an upward scroll, opens the history
view for that task — fetched from the endpoint in A and rendered as an ordinary
scrollable element. Because that element scrolls natively, the trackpad works
with full precision and correct momentum for free; there is no accumulator of
ours to tune, which is the main reason to prefer this over re-implementing
proportional scroll into the terminal.

The view opens scrolled to its bottom, so the transition from the live screen
reads as continuous upward scrolling. Returning to live is explicit — a control
plus `Escape` — and cheap, because the terminal was never detached and has stayed
current underneath.

The unattached tail block reuses the same view, which incidentally fixes the
~5-line-headroom problem without deepening the polled payload.

## C. Operator-adjustable height

`h-96` (xterm container, `Terminal.tsx`) and `max-h-80` (tail block,
`TaskPage.tsx`) are replaced by a store-driven inline height on a wrapper
carrying `resize-y` and `overflow-auto`, so the operator drags the bottom edge.

No backend change is required. `web/terminal.py` already applies `TIOCSWINSZ`
from the client's `resize` frames, and every viewer already gets its own
*grouped* tmux session (`new-session -t task-<N> -s view-<token>`), so window
sizing is already per-viewer: one operator's height cannot disturb another viewer
or the dispatcher. `Terminal.tsx`'s existing `ResizeObserver` already calls
`fit()` on container resize, so the drag propagates to cols/rows unmodified.

`useUiStore` (`frontend/src/store/ui.ts`) is in-memory only, so add zustand's
`persist` middleware scoped to the height field alone.

`terminalOpenFor` must **not** be persisted. Attaching writes the `attached-<N>`
marker and the dispatcher declines to drive a task while it exists; a persisted
attach would re-establish it on page load and stall a task. This is the hazard
the field's existing comment warns about.

## Rejected alternative: tmux copy-mode

The obvious alternative is `set -g mouse on` plus proportional scroll steps into
tmux copy-mode, giving true in-terminal scrollback. It was rejected because
grouped sessions share panes and copy-mode is pane state, not client state.
Measured on tmux 3.6a:

| Test                                          | Result                          |
| --------------------------------------------- | ------------------------------- |
| Viewer enters copy-mode → task session's pane | `pane_in_mode: 1` — leaked      |
| `capture-pane -p` while parked in copy-mode   | returns the **live** grid — safe |
| `window_activity` while parked                | still advances — stall detection safe |
| Kill the view session (browser detach)        | pane **stays** in copy-mode     |
| Dispatcher `send-keys` to a parked pane       | **silently not delivered**      |

The last two rows are disqualifying. Replies are delivered by `send-keys`
(`dispatcher/sessions.py:104-105`), so an operator who scrolls up and closes the
tab would silently break the reply channel for that task. It is fixable —
`tmux copy-mode -q -t task-<N>` clears the mode and restores delivery, verified —
but it means adding a tmux call to the reply path and to detach teardown to
guard a hazard this design simply does not create.

Recorded here so the option is not revisited without the evidence, and because
the same leak would affect any future feature that drives copy-mode.

## Testing

- `capture_history` builds the right `capture-pane -S` invocation and returns
  `""` on tmux failure.
- `capture_tail` is unchanged — a regression test asserts it still captures the
  visible pane only and still trims to 25 lines, so the dispatcher's
  classification inputs are provably untouched.
- `GET /api/task/{issue}/history` honours `lines`, clamps above 10000, and 404s
  for an unknown task consistently with the existing task routes.
- **The wheel handler emits nothing to the socket.** Given a wheel event over an
  attached terminal, no frame is sent — neither `ESC[<…M` nor `ESC O A`. This is
  the regression test for task 184's keystroke injection and is the most
  important test in this change; it is exactly what the instrumented measurement
  above checked by hand.
- An upward wheel over the live terminal opens the history view; `Escape` and the
  control both return to live.
- The tail block and xterm container render at the persisted height, and the
  store round-trips height **without** persisting `terminalOpenFor`.
- Manual, on the box: trackpad scrolling reads back through history smoothly on
  both a mouse-reporting session (162) and a non-mouse-reporting one (184), and
  dragging the terminal edge resizes it and survives a reload.
