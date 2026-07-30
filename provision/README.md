# Provisioning

`bootstrap.sh` sets up a fresh Ubuntu 24.04 dedicated VPS for agent-ops.
Box size: **2 vCPU / 4 GB** — see ADR 0002. Deliberately NOT shared with
the portfolio_eval prod VPS (OVH VPS-2): firewall model conflicts,
container isolation requirements, and OOM/disk blast radius won the
trade-off (ADR 0001). Ingress is Tailscale-only (UFW deny-all on the
public interface).

All agent-ops units are systemd USER units under the `agent` user
(~/.config/systemd/user/, `loginctl enable-linger agent`): dispatcher,
waitd, keepalive, digest, update. Root is used only at bootstrap
(packages, UFW, Tailscale, user + linger); convergence never runs as
root. `agent` is in no privileged group — the docker-group gap from
ADR 0001 is closed (Podman, not Docker, is used; no Docker daemon exists
on the box).

Bootstrap installs **Podman** and tmux; enables **zram swap (~2 GB)** for
the memory cushion that absorbs session RSS spikes and makes capacity 2
(the seeded default) survivable on 4 GB.

**Claude Code on the host is a native per-user install** (`claude.ai/install.sh`
run as `agent`, lands in `~/.local/bin/claude`), never a root `npm install -g`:
with the npm prefix at `/usr` the agent user can't write there, auto-update
fails ("no write permission to npm prefix") and the box silently pins to a
stale version. Login shells find it via `~/.profile`; systemd user units and
convergence scripts must reference `%h/.local/bin/claude` /
`$HOME/.local/bin/claude` explicitly — the user manager's PATH excludes
`~/.local/bin`. Bootstrap uninstalls any leftover root npm copy on re-run.
(The npm install inside the `agent-ops-session` container image is fine: the
image is rebuilt by the updater, nothing auto-updates in-place there.)

Deploys are pull-based convergence: an `agent-ops-update.timer` on the box
(every ~1 min) does `git pull --ff-only` against `main`, reinstalls the
package if deps changed, syncs systemd units, and restarts changed
services. The updater also rebuilds the `agent-ops-session` Podman image
when the `Containerfile` changes (tagged by content; running containers are
never touched mid-task). Updater and dispatcher pass share a flock so code
never swaps mid-pass. Merging to main (CI-gated) IS the deploy — no
CI→VPS push, no GitHub-held deploy credentials.

Claude sessions run in rootless Podman containers: the `agent-ops-session`
image (node + Claude Code CLI, git, gh, python/pipenv, pnpm) is launched
per task with the task worktree, `~/agent-ops-state/claude-home`
(Claude auth + transcripts), and gh credentials (read-only) mounted;
`--memory 1500m --cpus 2` (the memory cap comes from `session_memory`,
sized so two active sessions fit the 4 GB box). A tmux
session wraps each container for TTY persistence and reply-injection. E2E
runs off-box on GitHub Actions (`e2e.yml` in portfolio_eval, dispatched via
`gh workflow run`); there is no nested-container machinery on the box.

Park/resume: when a session needs human input or is waiting on a CI run,
the dispatcher stops the container, frees the slot, and resumes via
`claude --continue <message>` when the wake event fires (Telegram reply or
CI completion). No session ID is recorded; `--continue` reuses the most
recent transcript keyed by the worktree cwd, which is mounted at the same
path inside the fresh container. Woken tasks are always head-of-queue.

GitHub auth is split across two tokens: gh's stored auth is a fine-grained
PAT scoped to target repos (contents/issues/PRs/actions, nothing on
jesdi/agent-ops), and `gh project` calls use a classic PAT with only the
`project` scope (user-owned Projects v2 don't support fine-grained PATs),
delivered as GH_PROJECT_TOKEN via op run and injected per-command by the
dispatcher's GitHub adapter.

Runtime state lives in ~/agent-ops-state, outside the repo. That includes
the live targets.yaml: the repo ships targets.example.yaml only; the real
one is box-local — deliberately outside CI so ops knobs (capacity,
thresholds) are tunable without a merge cycle and the checkout stays clean
for ff-only pulls.

## Web console

`agent-ops-web.service` runs `python -m web` bound to **127.0.0.1:8481
only** — loopback is bypass prevention, not the access boundary. The
boundary is `tailscale serve --bg 8481`, which publishes
`https://<box>.<tailnet>.ts.net/` with a tailnet certificate and injects
the Whois identity headers (`Tailscale-User-Login`, …) on every proxied
request. The app 401s any request without that header, so nothing that
bypasses the proxy is served, and the header value is recorded as the
actor on every write. UFW stays deny-all on the public interface; Funnel
is never enabled.

Because the proxy injects that header on *every* request the operator's
browser makes — including ones a third-party page triggers — identity
alone does not prove first-party intent. Unsafe methods (anything but
GET/HEAD/OPTIONS) and WebSocket handshakes additionally require that any
`Origin` header match the request's `Host`; a mismatch is 403 (WS close
4403). Requests with no `Origin` at all — curl, and the SSE check below —
are unaffected.

After changing serve config or upgrading tailscale, re-verify the SSE
stream end to end from another tailnet device (NOT localhost):
`curl -N https://<box>.<tailnet>.ts.net/api/events` must show a comment
line within ~15 s. Buffering proxies silently break SSE; localhost tests
cannot catch it.

Deploys: update.sh try-restarts the service whenever pulled commits touch
`web/`, `dispatcher/`, `telegram/` or `pyproject.toml` (editable install —
new code is live on restart).

## Claude-home seed (ADR 0003)

`provision/claude-home/` is the versioned source of the box's Claude
config: `settings.json` (plugin declarations + guardrail hook wiring),
`CLAUDE.md` (box session conventions), `skills/` (box-authored process
skills), `hooks/` (box-variant git guardrail). `claude-home-sync.sh` —
run by update.sh every pass and by bootstrap.sh once — converges it into
`~/agent-ops-state/claude-home` with full authority inside those four
paths (deletes propagate: remove a skill from the seed and it disappears
from the box). Machine state is never touched: `.credentials.json`,
`projects/` transcripts, the `plugins/` cache.

Plugins: `settings.json`'s `enabledPlugins` values declare `true` (track
latest) or `"x.y.z"` (pinned). The updater installs/uninstalls/updates via
the `claude` CLI pointed at claude-home; a declared plugin that ends up
missing or off-pin fails the update pass loudly. Never install plugins on
the box by hand — declare them in the seed and merge.

Rollout order (from the design spec §9):
1. bootstrap.sh + the manual follow-ups it prints
2. dispatcher --dry-run against the live board
3. single live task (temporarily set capacity: 1) on one small `auto` task
4. park/resume drill — trigger a human-input park, reply from Telegram,
   verify context and reply arrive correctly
5. E2E drill — companion e2e.yml merged into portfolio_eval; dispatch a
   real run, verify awaiting-ci park → CI completion → resume with verdict
6. steady state
7. restore `capacity: 2` / `session_memory: 1500m` (the seeded defaults);
   retreat to capacity 1 on OOM kills or slow stages
