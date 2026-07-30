# Mobile interaction via Claude link-out

Date: 2026-07-30
Status: approved

## Problem

On mobile, the attached xterm terminal on the task page is effectively
unusable:

- Touch gestures never reach the scroll-up-opens-history mechanism —
  `attachCustomWheelEventHandler` only fires for wheel events, which phones
  do not emit. Touch drags on the xterm canvas either do nothing or bubble
  up and scroll the whole page.
- Typing into an embedded xterm from a phone keyboard is poor regardless.

On desktop there is a related UX gap: an upward flick opens the
`TerminalHistory` overlay, and the only way back to the live interactive
terminal is a small "Back to live" corner button. Operators miss it and
resort to detach + attach to recover the keyboard.

The detached `pane_tail` preview works fine on mobile and is unchanged.

## Decision

Do not build touch support into the embedded terminal. Every box session is
already launched with `--remote-control task-<N>` (`dispatcher/containers.py`),
so it is reachable from claude.ai/code and the Claude mobile app's Code tab,
identifiable by name. Link out to that first-class UI for mobile
interaction; keep the embedded terminal as the desktop escape hatch, with
one minimal fix to its history overlay.

Alternatives considered:

- **Link-out only (remove the embedded terminal):** rejected — Remote
  Control needs periodic re-login on the headless box, so the link can go
  stale; the embedded terminal stays as the fallback and raw-TTY escape
  hatch.
- **Fix touch gestures + unify live/history styling in the embedded
  terminal:** rejected — large effort to approximate what the Claude app
  already does natively.
- **Capture the per-session Remote Control URL for a one-tap deep link:**
  deferred — start with the naming convention; upgrade later if the extra
  tap (picking the session by name) proves annoying.

## Design

### 1. "Open in Claude" link on the task page

- An anchor `Open in Claude ↗` to `https://claude.ai/code`, with
  `target="_blank"`, placed with the terminal controls next to
  "Attach terminal".
- The operator picks the session by name (`task-<N>`); on the phone the
  same flow lives in the Claude app's Code tab.
- Rendered regardless of `session_alive` — claude.ai/code is also where a
  dead session's conversation remains readable.
- No dispatcher or read-model changes. No URL capture.

### 2. History overlay auto-return

- In `TerminalHistory`, a scroll listener on the pane: when the user
  scrolls back to the bottom (threshold ≤ 4 px) *after having been above
  it*, call `onClose()` — restoring the live interactive terminal,
  native-scrollback style.
- The "arm after leaving the bottom" guard is required because the pane
  opens scrolled to the bottom; without it the overlay would close on
  mount.
- Escape and the existing "Back to live" button keep working.

### Out of scope

- Touch-gesture handling on the xterm terminal (mobile path is the Claude
  app).
- Styling unification between the live terminal and the history view.
- Per-session deep-link capture (deferred upgrade).
- The `resize-y` drag handle on mobile.
- Surfacing the Remote Control re-login caveat in the UI.

## Testing

- Component tests:
  - Task page renders the link with the correct href and `target="_blank"`.
  - History pane closes when scrolled to bottom after having scrolled up.
  - History pane does not close on mount (opens at bottom without firing
    `onClose`).
- Manual E2E from a phone against the box console: open task page → tap
  link → Claude app Code tab → find `task-<N>` → interact.
