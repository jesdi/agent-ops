# herdr as the session layer (tmux → herdr) — design

Date: 2026-09-03
Status: approved (brainstorm 2026-09-02/03; box spike 2026-09-03)

## Problem

Every box session today lives in a tmux session the dispatcher creates
(`dispatcher/sessions.py`: one `task-<target>-<issue>` per task, a
`podman run … claude` per stage inside it). tmux is a generic multiplexer:
it knows a pane changed (`window_activity`), not what the agent in it is
doing. Three consequences:

1. **Stall and blocked detection are heuristics.** "Idle for
   `stall_after_seconds`" is the only signal that a session is stuck; a
   permission/trust/login dialog with nobody attached looks the same as
   thinking. The `/login` case needed its own screen classifier
   (`dispatcher/relogin.py`).
2. **Operator access is bespoke.** The board embeds its own PTY bridge
   (`web/terminal.py`, `Terminal.tsx`, grouped tmux view sessions, the
   `attached-*` hold marker) because plain tmux has no mobile story. That is
   ~200 lines of concurrency-sensitive code plus a frontend component, and
   the only attach path the dispatcher knows about — an operator in
   `ssh + tmux attach` gets no protection at all.
3. **Nothing on the box shows the fleet.** An operator at a terminal sees
   tmux session names; agent state is only on the board.

[herdr](https://herdr.dev) (0.8.2, Apache-2.0, Rust) is an agent-aware
multiplexer: it detects the coding agent in each pane and exposes its
lifecycle (`idle | working | blocked | done | unknown`) and a JSON socket
API (`herdr pane …`, `herdr agent …`) covering everything `sessions.py`
does with tmux. A spike on the box (2026-09-03) confirmed the parts that
matter for us:

- A Claude running **inside `podman run -it`** is detected when the host
  wrapper command carries `HERDR_AGENT=claude` (herdr reads the pane's
  foreground process env; the wrapper hides the real binary otherwise).
  The trust prompt classified as `blocked`; after `send-keys enter`,
  `agent wait --until idle` returned `idle`.
- `pane run` / `pane read` / `pane send-text` / `pane send-keys` map onto
  `send-keys` / `capture-pane` / `send-keys -l`.
- `herdr agent attach <pane>` under a PTY renders a single pane — the
  equivalent of a grouped tmux view session — and `herdr --remote box`
  from a Mac terminal attaches to the box server over plain SSH.
- The server survives SSH disconnects; 10 MB scrollback per pane.

## Decisions (agreed)

- **herdr replaces tmux as the session layer on the box.** Sessions,
  the triage sweep, and operator attach all go through the herdr server
  running as user `agent`. tmux is retired from the box once no legacy
  session remains (see §7).
- **The `Sessions` interface does not change.** `is_alive`, `spawn_stage`,
  `resume`, `capture_tail`, `capture_history`, `idle_seconds`,
  `send_text`, `end` keep their signatures and degrade contracts. The
  meaning of `is_alive` narrows (busy shell, not merely present), which
  routes exited clades through `HandleCrash`; `HandleCrash` now also calls
  `sessions.end()` to snapshot and close the dead tab.
- **The board's live terminal is removed, not ported.** Operator attach
  is externalised: `herdr --remote box` from a desktop terminal,
  [Moshi](https://getmoshi.app) (herdr-aware iOS/Android client over SSH
  via the tailnet) from a phone. The board keeps the read-only console
  view (pane tail + scrollable history, live or snapshot).
- **The attach hold is dropped, not re-implemented.** `attached-*`
  markers and the five `has_attached` gates in `main.py` go away.
  Known limitation: the dispatcher may park (stop the container) while
  an operator is typing in an attached pane — recoverable via the
  designed reply path (Telegram / board message → `--continue`).
  Operating rule: *attach to watch; reply through Telegram or the board.*
  If this bites, the follow-up is a hold keyed on herdr's pane `focused`
  state, which would also cover Ghostty/Moshi attaches that were never
  protected under tmux.
- **In-flight tasks migrate once, at deploy, through park/resume.** The
  updater ends every live tmux session and marks its task
  `unpark-requested`; the next pass resumes each in herdr with
  `claude --continue` (one interrupted turn per task, once). Deploys stay
  pull-based; no drain, no dual backend.

## 1. herdr topology and naming

| herdr object | agent-ops meaning | label |
|---|---|---|
| server (default session) | the box | — |
| workspace | one per target, plus one `agent-ops` system workspace | `<target>` / `agent-ops` |
| tab | one per task (or the triage sweep) | `task-<target>-<issue>` / `triage` |
| root pane | the shell that hosts `podman run … claude` (or the sweep) | — |

Workspaces are created on first use (`herdr workspace create --label
<target> --cwd <clone root> --no-focus`) and never closed by the
dispatcher. Tabs are created at spawn and closed at `end()`. All herdr IDs
(`w1`, `w1:t3`, `w1:p3`) are opaque and are **never persisted**: every
call resolves `(target, issue)` → tab → root pane by label, exactly as
`tmux has-session -t name` resolves by name today. A missing tab means
"dead", the same as a missing tmux session.

Resolution is one `herdr tab list --workspace <w>` + one `herdr pane list
--workspace <w>` (root pane = the pane whose `tab_id` matches). Both are
local socket calls; the dispatcher pass already makes several tmux calls
per task, so cost is comparable.

## 2. `dispatcher/sessions.py` — the herdr backend

`Sessions` is one class over `herdr.Tab`. Liveness = the tab exists and
its shell is busy (`Tab.alive`); a claude that has exited back to the host
shell reads dead, and a tab restored after a herdr server restart reads
dead. `HERDR_AGENT=claude` is tab-level `--env` set by `Sessions._launch`;
it never appears in `containers.session_cmd` — the headless triage
container shares that module and must not read as an agent.

| method | herdr calls | notes |
|---|---|---|
| `is_alive` | tab exists and shell busy (`Tab.alive`) | a claude that has exited back to the host shell reads dead; crash path, not stall |
| `_launch` | `Tab.ensure(target, name, worktree, env={"HERDR_AGENT": "claude"})` → close any non-busy existing tab and open a fresh one; then `tab.run(podman_cmd(...))` | `HERDR_AGENT` is set as tab-level `--env` by herdr, never typed in the command; `containers.session_cmd` is unaware of it |
| `spawn_stage`, `resume` | unchanged: write the prompt file, call `_launch` | |
| `capture_tail(lines)` | `pane read <pane> --source visible --lines <lines>` | tail of the rendered viewport, as `capture-pane -p` today |
| `capture_history(lines)` | `pane read <pane> --source recent-unwrapped --lines <lines>` | console history; soft wraps joined (an improvement over tmux, which hard-wraps and made `relogin.py` rejoin lines) |
| `idle_seconds` | `pane get <pane>` → `agent`, `agent_status`, `state_change_seq` | see §3 |
| `send_text(text)` | `pane send-text <pane> <text>`; `pane send-keys <pane> enter` | literal text then a separate Enter, as today |
| `end` | `_snapshot()`; `tab close <tab>`; `podman rm -f <name>` (+ legacy `task-<issue>`) | closing the tab HUPs the shell and the `-it` podman; the explicit `rm -f` stays as the belt-and-braces it is today |

Every herdr invocation is `subprocess.run([...], capture_output=True,
timeout=30)`. The CLI reports server errors as JSON on stderr with exit 1
and syntax errors with exit 2; the backend treats any non-zero exit
exactly as it treats a tmux failure today: `is_alive → False`,
`capture_* → ""`, `idle_seconds → None`, mutations best-effort. A herdr
server that is down therefore reads every task as dead — the same
failure mode as a dead tmux server, but now behind a `Restart=always`
unit (§6) rather than a server that is only ever started as a side
effect of a spawn.

## 3. Liveness and stalls: `agent_status` instead of `window_activity`

`idle_seconds` keeps its contract — *seconds the session has been
static; `None` = unknown* — so `machine.py`'s stall rule is untouched.
The herdr backend computes it from the agent lifecycle rather than from
screen activity:

- `agent_status == "working"` → `0.0` (not idle, regardless of how long).
- Anything else (`idle`, `blocked`, `done`, `unknown`, or no agent at all —
  e.g. the pane is back at the host shell) → seconds since the pane last
  changed status.

A claude that exits back to the host shell reads dead (`is_alive` = False)
and goes to `HandleCrash` (`_report_session_crash`), no longer stall-park
after `stall_after_seconds`. This is the truthful classification: the
container is gone and the session needs a fresh `claude --continue`, not a
stall signal.

herdr exposes the transition counter (`state_change_seq`) but not a
timestamp, so the backend keeps a sidecar
`<state_dir>/herdr-status/<session name>.json` = `{"seq", "status",
"since"}`: if `(seq, status)` matches the stored pair, return
`now - since`; otherwise overwrite with `since = now` and return `0.0`.
The sidecar is removed in `end()` alongside the snapshot write, and a
read/write failure returns `None` (unknown), never a stale number.

This is strictly more accurate than tmux's heuristic: Claude Code's
status line redraws every second while working, which is what made
`window_activity` usable, but a `blocked` dialog also redraws its cursor
and could read as "active". Using `blocked` for anything smarter than
"idle time accumulates" (e.g. parking immediately with the dialog text)
is a follow-up, not part of this change.

## 4. Web console: read-only

Removed:

- `web/terminal.py` (PTY bridge, `AttachRegistry`), the
  `/api/task/{target}/{issue}/terminal` websocket route, and their tests.
- `frontend/src/components/Terminal.tsx`,
  `hooks/usePersistedTerminalHeight.ts`, the terminal-height UI store
  state, and their tests; `TaskPage.tsx` renders the pane tail where the
  live terminal was, with `TerminalHistory` as the scrollable view.
- `state.mark_attached / has_attached / clear_attached`, the
  `attached-*` glob in `web/sources.py`'s change digest, `attached` in the
  task-card read model and its frontend rendering, and the five
  `has_attached` gates in `dispatcher/main.py` (`_flush_done`,
  `_resume_woken`, `_spawn_feedback`, `_drive_task`, pr-open teardown).
  `terminal-attach` / `terminal-detach` event kinds stop being emitted;
  existing history rows keep rendering.

Kept, unchanged: `sources.pane_tail` / `pane_history` /
`session_alive`, the `/history` route, the snapshot fallback for dead
sessions, `TerminalHistory.tsx`. The task page gains one line of
operator guidance next to the console: `herdr --remote box` (desktop) /
Moshi (phone) — text only, no deep links (Moshi's
`moshi://herdr?workspace=` needs the workspace id, which the board does
not track; add it if the plain picker proves annoying).

## 5. Triage sweep

`dispatcher/triage.py` moves from a detached tmux session to a tab
labelled `triage` in the `agent-ops` workspace: `running()` = the
`triage` tab is alive (exists and its shell is busy); no tmux fallback.
Launch = `tab create --workspace <w> --label triage --cwd <repo>
--env VAR=value …` then `pane run <pane> "<python> -m dispatcher.main
--triage-run …"`. `--env` carries `LAUNCH_ENV_VARS` exactly as tmux's
`-e` did — the herdr server's environment is the user unit's, which has
no Telegram credentials, so the explicit forwarding remains necessary
for the same reason the existing comment gives.

The sweep itself is a headless `claude -p` inside podman; herdr will show
it as a plain pane (no `HERDR_AGENT`), which is accurate.

## 6. Provisioning and units

- **Binary.** `provision/bootstrap.sh` installs herdr for `agent` via the
  official installer into `~/.local/bin` (already on `.profile`'s PATH)
  and asserts `herdr --version`. `provision/update.sh` gains an "ensure
  herdr" step that runs the same installer when the binary is missing —
  the pass has already pulled code that needs it, so unlike the pnpm
  check (warn and skip) the converging move is to install; the installer
  is idempotent, sudo-free and writes only under `agent`'s home. If the
  install fails the pass aborts before the unit sync, as a missing pnpm
  does today. No version pin beyond "whatever the installer resolves at
  install time"; the box updates herdr only when an operator runs
  `herdr update` (background `version_check` only notifies).
- **Server unit.** New `provision/agent-ops-herdr.service` (user unit,
  synced by `update.sh` like every other `provision/*.service`):
  `ExecStart=%h/.local/bin/herdr server`, `Restart=always`,
  `WantedBy=default.target`. Every session pane, its shell and its podman
  process are children of this server, so they live in this unit's
  cgroup: **stopping or restarting the unit kills every live session**,
  the same as killing the tmux server today. Document it in the unit and
  in `provision/README.md`; the sessions are recoverable (park/resume
  already tolerates a dead container via `claude --continue`).
- **Dependent units.** `agent-ops-dispatcher.service`,
  `agent-ops-web.service`, `agent-ops-waitd.service`,
  `agent-ops-triage.service`: `After=agent-ops-herdr.service`,
  `Wants=agent-ops-herdr.service`.
- **`KillMode=process` goes.** The dispatcher unit carries that setting
  (and a long comment) only because a oneshot pass spawned tmux servers
  that had to outlive it. Panes now belong to the herdr unit, so the
  default `KillMode` is correct again and the comment is deleted.
- **tmux stays installed only so §7's one-shot migration can find
  sessions.** After migration: delete the `apt-get install … tmux …` line
  in `bootstrap.sh` together with `dispatcher/tmux_migration.py` and the
  guarded hunk in `update.sh`. The "run bootstrap inside tmux or mosh"
  advice is about bootstrapping the box, not about runtime sessions, and
  stays.
- **Docs.** `README.md` / `provision/README.md` / `CONTEXT.md` replace
  "tmux session wraps each container" with the herdr wording and add
  the operator attach recipe (`herdr --remote box`; Moshi).

The box already has herdr 0.8.2 at `~agent/.local/bin` and a
hand-started `herdr server` from the spike, owning the default socket.
The plan's deploy step stops that process (`herdr server stop`) right
before the unit is enabled, so the unit's server is the only one on the
socket.

## 7. In-flight migration: one-shot park/resume at deploy

`dispatcher/tmux_migration.py` + `python -m dispatcher.main --migrate-tmux`
do the migration; `update.sh` calls it once in a guarded hunk (idempotent
— the hunk is skipped once no tmux sessions remain). Per live task:

1. `tmux kill-session` on both the `task-<target>-<issue>` and legacy
   `task-<issue>` names.
2. `podman rm -f` both container names.
3. `_wake(cfg, task, MESSAGE)` queues the message and sets `park=PARK_WAKE`
   — the same state a normal reply-wake leaves a task in.

A live `triage` session is killed and the sweep re-enqueued as a fresh
request.

`_resume_woken` in `main.py` resumes each `unpark-requested` task on the
next pass via herdr with `claude --continue`. Cost: one interrupted turn
per in-flight task, once.

**Retirement** (pure deletion, no live callers): delete
`dispatcher/tmux_migration.py`, the `--migrate-tmux` flag branch in
`main.py`, the guarded hunk in `update.sh`, the migration tests, and the
`apt-get install … tmux …` line in `bootstrap.sh`. Do this once the box
confirms no `task-*` tmux session remains (`tmux ls` on the box, or
simply: every task active at deploy time has ended — expected within a
day).

## 8. Error handling

- Degrade contracts are the tmux ones, verbatim (§2). No new exception
  types cross the `Sessions` boundary.
- `_launch` failure (tab created, `pane run` failed) leaves a tab with a
  bare shell — not busy — which reads dead. The crash path owns it.
- A herdr server restart restores every tab as a fresh shell wearing its
  old label. The dispatcher reads it dead (liveness = busy shell, and a
  bare shell is not busy) → crash path (`_report_session_crash`), as for
  any dead session. The next `_launch` for that task calls `Tab.ensure`,
  which closes the bare-shell tab before creating a fresh one.
- A tab whose label exists twice (should never happen: labels are
  created only by the dispatcher, and only when absent) resolves to the
  first match; `end()` closes only that one. Not defended further.

## 9. Testing

- **Unit (`tests/test_sessions.py`).** Fake `subprocess.run` recording
  argv. Cover: `Tab` (`find` / `ensure` / `alive`, including the
  restored-bare-shell case); `Sessions` over `Tab` (`HERDR_AGENT=claude`
  as tab-level `--env`, never in the command string; label resolution;
  `is_alive` = busy shell); `idle_seconds` from `(seq, status)` with the
  sidecar (working → 0, same pair → elapsed, new pair → 0, missing agent
  → accumulates, sidecar error → `None`); degrade on exit 1 / exit 2 /
  timeout; `end()` ordering (snapshot before close; sidecar removed).
- **Unit (`tests/test_tmux_migration.py`).** Cover: kill/rm/wake order
  per task; triage re-enqueue; missing tmux binary → no-op (the `_run`
  helper returns `None`, migrate returns `[]`); update.sh guard runs the
  migration only while `tmux ls` succeeds.
- **Triage (`tests/test_triage.py`).** `running()` = the `triage` tab
  exists and its shell is busy (alive); launch argv carries `--env` for
  each set `LAUNCH_ENV_VARS`.
- **Web / dispatcher.** Delete terminal and attach tests; the fakes in
  `tests/test_main.py` / `tests/test_web_sources.py` drop
  `attached`; `_drive_task` etc. no longer read markers.
- **Frontend.** Delete `Terminal`/`usePersistedTerminalHeight` tests;
  `TaskPage` test asserts the console view and guidance line.
- **Provisioning.** `tests/test_credentials_script.py`-style shell test
  for `update.sh`'s ensure-herdr branch (present / missing).
- **Box verification (plan step, manual).** After the first converged
  deploy: one task spawned end-to-end in herdr; `herdr --remote box`
  shows it under the target workspace with `working`; park → resume
  lands in herdr; in-flight tmux tasks at deploy time are parked and
  resume in herdr on the next pass.

## Out of scope / follow-ups

- Moshi setup on the phone and `moshi-hook` on the box (notifications):
  operator-side; a `provision/README.md` recipe at most.
- Using `blocked` semantically (park immediately with the dialog text;
  answer trust/login prompts) — replaces `relogin.py`'s screen
  classifier; separate spec.
- Attach hold keyed on herdr `focused` — only if the dropped hold bites.
- herdr's Claude integration (`herdr integration install claude` writes
  Claude hooks that report session identity to the socket): would need
  the herdr binary and socket inside the container. Screen detection is
  sufficient; revisit only if `[session]` resume-after-restart becomes
  worth having.
- Local (Mac) herdr for desktop agents: unrelated to the box.
