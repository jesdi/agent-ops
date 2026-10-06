# agent-ops

**Your backlog gets worked while you sleep. You review from your phone.**

agent-ops turns a small dedicated VPS into an autonomous engineering box: a
dispatcher drains your GitHub-hosted backlog by running sandboxed Claude Code
sessions around the clock, spending your subscription's tokens *deliberately*
instead of letting them expire, and reporting back over Telegram and a mobile
web console. Your PC stays off.

## Why

If you develop with Claude Code on a subscription plan, you have two standing
inefficiencies:

- **Your nights are idle.** The agent only works while you're at the keyboard,
  so 12+ hours a day of potential throughput is simply lost.
- **Your token windows go to waste.** Usage you don't spend before the window
  resets is gone. Unused capacity at the end of a window is a sunk cost.

agent-ops closes both gaps:

- **Asynchronous overnight work.** File tasks into the backlog during the day;
  the box specs, plans, and implements them through the night, each stage in a
  fresh sandboxed session, ending in a CI-gated PR waiting for your morning
  review.
- **Pace-aware spawning.** Before every spawn the dispatcher reads each
  provider's usage windows — the 5-hour session, the week, and any
  per-model week such as Fable's — and admits a model only while every
  window it draws on has headroom. The session window keeps its ceiling
  (relaxed when the reset is close). The weekly windows follow a spending
  schedule that counts weekend hours at half weight, so the box holds back
  on Saturday and Sunday and spends the saved share Monday to Friday. A
  provider whose usage can't be read fails safe and spawns nothing. Which model runs is a **track**: triage picks one for the spec stage, the spec session picks one for the rest, and each track lists models per stage, so the box takes the one with headroom whose weekly quota most needs spending before its reset and keeps it for the whole stage. A session-bound provider's entries go first, and a priority mode set in the console (`auto` or a provider) replaces this order (see CONTEXT.md).
- **24-hour access from your phone, PC off.** The box is reachable over
  Tailscale only. The web console and Telegram bot are always on — you can
  check progress, answer an agent's question, or approve a plan from anywhere.
- **A board that answers "what needs me?" at a glance.** The board is two
  zones. **Needs you** comes first — Needs review, PR review, Parked, Failed,
  Stalled on budget — and everything in the pipeline zone (Queued, In
  progress, Awaiting CI, Resuming, Done, Wont do) is the box's problem.
  Queued shows the ranked, not-yet-claimed issues as ghost cards behind the
  claimed ones. Empty columns collapse into a strip of zero-count chips, so
  "no failures" is visible rather than missing. On a phone the board shows
  one column at a time through tabs. Capacity and a usage panel sit above
  the board so you always know how hard the box is working: every provider's
  windows with their headroom, and one gate saying whether the box would
  spawn its default model right now.

## How it works

The guiding rule: **GitHub is the state store; the box is the compute.** There
is no CI→VPS RPC. The deployment repository selects the application revision
and configuration that the box pulls.

```mermaid
flowchart LR
    subgraph github["GitHub — state store"]
        board["Backlog<br/>(issues + project board)"]
        repos["Target repos<br/>(branches, PRs, CI)"]
        main["agent-ops main"]
    end

    subgraph box["The box — compute (VPS, Tailscale-only)"]
        dispatcher["Dispatcher"]
        session["Sessions<br/>(podman + herdr, one per task)"]
        console["Web console"]
        telegram["Telegram bot"]
        updater["Updater timer"]
    end

    phone(["Your phone"])

    dispatcher -- "poll & rank" --> board
    dispatcher -- "spawn / park / resume" --> session
    session -- "branches, PRs" --> repos
    updater -- "git pull --ff-only (~1 min)" --> main
    phone <--> console
    phone <--> telegram
    console --> dispatcher
    telegram --> dispatcher
```

```mermaid
flowchart LR
    queued["Queued"] --> gate{"Usage (pace) &<br/>capacity gate"}
    gate --> spec["Spec stage<br/>(proposal, spec)"]
    spec --> plan["Plan stage<br/>(design, tickets)"]
    plan --> review{"Plan review gate<br/>(from your phone)"}
    plan -- "gate skip" --> implement
    review -- feedback --> plan
    review -- approved --> implement["Implement<br/>(one session, every ticket)"]
    implement -- every ticket merged --> codereview["Review stage<br/>(review, ADRs, only spec.md stays,<br/>rebase, gates, PR)"]
    codereview --> pr["PR open"]
    pr -- "CI green, you merge" --> done(["Merged"])

    spec -. "needs input" .-> parked["Parked<br/>(slot freed)"]
    plan -. "needs input" .-> parked
    implement -. "needs input" .-> parked
    codereview -. "needs input / cap hit" .-> parked
    parked -. "your reply / CI event" .-> queued
```

- **Dispatcher** (`dispatcher/`) — polls the backlog, ranks it, selects the
  next task within usage headroom and capacity limits, provisions a git worktree, and
  launches a Claude Code session for it. Capacity is box-wide: work already in
  flight runs first, then each free unit goes to the target with the fewest
  active tasks.
- **Staged pipeline** — each task moves through **spec → plan → implement (one session works every ticket) → review**,
  each stage a fresh session whose only input is the previous stage's committed
  artifact. The plan stage pauses at one human review gate, where you read
  the spec and the plan, before implementation spends real tokens on them. The dispatcher skips that gate
  only for a task on a track with `plan_review: false` whose spec and plan
  sessions asked no questionnaire and whose plan has no open question. An
  `untracked:` default that names such a track makes every unlabelled task
  gate-free.
- **Bounded loops** — review fixes, end-to-end fixes and CI fixes on an open
  PR each have a configured cap; hitting it parks the task and pings you, it
  never fails the task. The implement session's own fix loops are bounded by
  the `implement-spec` skill, not by the dispatcher.
- **Skills per stage** — spec: `to-openspec` stage 1 (`proposal.md`,
  `spec.md`), with `to-questionnaire` and `prototype` when a decision is
  open. Plan: checks stage 1, then `to-openspec` stage 2 (`design.md`,
  tickets). Implement: `implement-spec`. Review: `review-diff`, then the
  session moves the why and the decisions into the PR description, ADRs
  (`docs/adr/`) and `CONTEXT.md`, and removes `proposal.md` and `design.md`.
  Main keeps only `spec.md` of a change: `specs/` is a change log, the code
  and `CONTEXT.md` are the present state.
- **Sessions** run in rootless Podman containers (the `agent-ops-session`
  image: Node + Claude Code CLI, git, gh, Python/pipenv, pnpm), one per task,
  each in a tab of the box's [herdr](https://herdr.dev) server — the agent-aware multiplexer that gives the dispatcher the agent's real lifecycle (`working` / `idle` / `blocked`) instead of screen-activity heuristics, plus TTY persistence and reply injection. Sessions are
  disposable; state lives in artifacts and the persistent claude-home.
- **Park / resume** — when a session needs human input or is waiting on a CI
  run, the dispatcher stops the container, frees the slot, and resumes via
  `claude --continue <message>` when the wake event fires (a reply from you or
  CI completion). Woken tasks jump to the head of the queue — a paused agent
  never blocks a slot, and your answer never waits in line.
- **Deployment separation** — application code lives here; the operator's
  infrastructure repository owns systemd units, provisioning, session images,
  credentials integration, and the selected application revision.

### Daily backlog triage

An `agent-ops-triage.timer` fires at 07:30 Europe/Madrid and enqueues a triage
request; the dispatcher runs it in a real capacity slot (skipping the day if no
slot frees within 2 h). Per repo, a **read-only** Claude session reads the
issues touched since that repo's cursor and records decisions to a file;
deterministic Python (`dispatcher/triage_apply.py`) is the only GitHub write
path, applying labels and author questions. Closes are never executed — only
suggested in the single Telegram report that closes the sweep.

**Prerequisite — once per triaged repo, before its first sweep.** Triage
records `auto` (routine enough to automate) or `human-required` (needs heavy
human interaction) on the issues it judges, and a label that does not exist in
the repo's inventory is rejected rather than created — the sweep never mutates
label taxonomy. Create them up front, or the first sweep's report is a wall of
`unknown label(s) ['auto']` rejections and nothing gets labeled:

```bash
gh label create auto --repo OWNER/REPO \
  --description "Routine enough for an agent to take unattended" --force
gh label create human-required --repo OWNER/REPO \
  --description "Needs heavy human interaction; not agent-ready" --force
```

## Two ways to drive it from your phone

**Web console** (`web/` + `frontend/`) — the board view above, plus per-task
pages with the stage timeline, the spec awaiting your approval, a persistent Artifacts section, and a read-only console (pane tail plus scrollable history, snapshot-backed once the session ends). To interact with a session, attach from a terminal: `herdr --remote box` on the desktop (herdr installed locally, same version as the box, `box` an SSH alias over the tailnet), or [Moshi](https://getmoshi.app) on a phone. Operating rule: attach to watch; reply through Telegram or the board — the dispatcher may park a session while you are typing in it. Reply to
a parked agent, park, kill, retry, or resume a task, and manage the queue —
including model-capacity warnings on queued and claimed tasks, with one-shot
controls to run despite that usage limit or use another entry of the task's track —
all from the same UI. Failures and history get their own pages, so nothing
silently disappears.
The **Task states** link in the console's nav opens a reference of every stage, park and
board column ([`frontend/public/task-states.html`](frontend/public/task-states.html)).

The board renders saved task cards and capacity from `/api/board/snapshot`
while `/api/board` fetches live queue rankings (the ghost cards in Queued) and
the claim forecast. Cards may reorder when scores arrive; a loading or
unavailable message stays visible until live details are available. Secondary pages load when opened to keep the
initial board download small.

**Telegram** (`telegram/`) — outbound notifications and digests, plus inbound
replies that feed straight back into parked sessions. Queue control from the
same chat: `/queue` shows the ranked backlog; `/boost N [k]` / `/demote N [k]`
adjust an issue's priority band; `/next N` enqueues an issue at the head
(`/next N force` also makes it Ready + `auto`; blocked and in-progress issues
are never forceable).

## Layout

| Path            | What lives there                                              |
| --------------- | ------------------------------------------------------------ |
| `dispatcher/`   | Backlog polling, task selection, usage gate, sessions, state |
| `web/`          | FastAPI backend for the console (board, usage, terminal WS)  |
| `frontend/`     | React SPA — board, task pages, queue, failures, history      |
| `telegram/`     | Outbound notifications/digests and inbound reply handling    |
| `prompts/`      | Stage prompts (spec, plan, implement, review, address-review) and the triage prompt   |
| `tests/`        | pytest suite                                                 |
| `targets.example.yaml` | Template for the box-local `targets.yaml` (capacity, thresholds, model policy) |

## Development

Requires Python ≥ 3.11.

```bash
./scripts/setup.sh    # .venv, Python and frontend deps, agent skills
pytest
make gate             # before pushing: both suites, api-types, build, CRAP gate
```

Start new work with `./scripts/new-worktree.sh <branch> [base]`. It creates `.worktrees/<branch>` from the latest `origin/<base>` (default `main`) and runs setup there, so a new worktree always has its skills. Setup also enables the committed git hooks (`core.hooksPath .githooks`). Their `post-checkout` hook installs the skills in any new worktree, including a plain `git worktree add` or one Claude Code creates.

### Agent skills

Two kinds of agent skills live in this repo:

- **Committed:** the [mattpocock/skills](https://github.com/mattpocock/skills) set in `.agents/skills/`, pinned by `skills-lock.json`.
- **Not committed:** the skills from [jesdi/general-skills](https://github.com/jesdi/general-skills) (backlog, crap-gate, implement-spec, review-diff, to-spec, to-tickets, …). Only their list, `.my-skills.json`, is committed. The skills are gitignored and installed into `.my-skills/`, with symlinks in `.claude/skills/`.

Global Claude Code config (rules, hooks, and plugins like engram, ponytail and codex, which lets Claude hand work to Codex) lives in the private [jesdi/claude-config](https://github.com/jesdi/claude-config). Run its `install.sh` once per machine.

`scripts/setup.sh` installs the project skills. To install only them (needs Node and pnpm), run `./scripts/install-skills.sh`.

To add a new one, run `pnpm dlx @jesdi/skills-cli install <skill> --agent claude` and add its `.claude/skills/<skill>` line to `.gitignore`.

## Deployment

Use `targets.example.yaml` as a schema example for your own configuration.
Run the dispatcher with `python -m dispatcher.main --config /path/to/targets.yaml`
and the console with `python -m web --config /path/to/targets.yaml`.

The maintained VPS deployment has moved to the private
[`jesdi/agent-ops-infra`](https://github.com/jesdi/agent-ops-infra) repository.
It owns the host configuration, bootstrap/updater, systemd units, session image,
Claude-home seed, and operational runbooks. Access requires repository permission.

### Deploying the openspec pipeline

Done once. `<state_dir>` is the `state_dir` of the box's `targets.yaml`. Run
every command as the dispatcher's user, from the dispatcher's checkout, with
its virtualenv python (`.venv/bin/python`).

1. **Seeds.** Make sure the Claude-home seed and the Codex-home seed in the
   infra repository carry the `implement-spec`, `to-openspec` and
   `red-team-data-model` skills: the stage prompts read them from
   `~/.claude/skills/` and `~/.codex/skills/`.
2. **Drain, before the deploy.** Let every task in plan, implement or review
   finish. Tasks in the spec stage, at the spec review gate, or with an open
   pull request can stay. This check needs no new code; it prints `[]` when
   the box is drained:

   ```
   .venv/bin/python -c "import sys; from dispatcher.state import load_all; print([(t.target, t.issue, t.stage.value) for t in load_all(sys.argv[1]) if t.stage.value in ('plan', 'implement', 'review')])" <state_dir>
   ```

3. **Stop.** Stop the dispatcher and the updater, and keep them stopped
   until step 8:
   `systemctl stop agent-ops-dispatcher.timer agent-ops-dispatcher.service agent-ops-update.timer`.
   `agent-ops-web.service` and `agent-ops-waitd.service` can stay up: they
   write no task file. Stop `agent-ops-triage.timer` too if you want a quiet
   box; a triage sweep writes no task file either. The unit names are those
   of the infra repository and were not verified from this repository: check
   them with `systemctl list-units 'agent-ops-*'`. Run the check of step 2
   once more: a task can enter plan between the check and the stop.
4. **Back up.** `cp -a <state_dir> <state_dir>.pre-openspec`
5. **Deploy** the new code. Do not start anything.
6. **Check.** `.venv/bin/python -m dispatcher.openspec_migration --check <state_dir>`
   reads only: no lock, no write. It prints what step 7 would do and has the
   same exit status.
7. **Migrate.**
   `.venv/bin/python -m dispatcher.openspec_migration <state_dir> | tee openspec-migration.log`
   Keep the output: a second run prints every task as `untouched`.
8. **Start** the units of step 3.

Output, one line per task, then a summary line:

| Label | Meaning |
|---|---|
| `converted` (`would-convert` with `--check`) | Was in the spec stage or at the old spec review gate. It starts a fresh spec session on the next pass. The operator's earlier messages for it are queued again and arrive with the new session's prompt. |
| `untouched` | Not changed: an open pull request, a finished task, a task of the new flow. `(failed, not resumable)` is a failed task that Resume cannot start, for example one that failed at the old spec review gate: move its issue back to Ready to start it again. |
| `do-not-resume` | An old task that failed in plan, implement or review. Do not use Resume on it: a new session would work on the old artifacts. Cancel it, or move its issue back to Ready. It is marked so that it can never skip the plan review gate. |
| `must-drain` | An old task still in plan, implement or review. The run is refused. |
| `not converted` | Would be converted, but the run is refused. |
| `unreadable` | A task file or a message file that cannot be read. The run is refused. |
| `unknown-value` | A task file with a stage, park or request that the command does not know; the line names the file, the field and the value. The run is refused. Do not delete the file. |
| `stopped at <file>` | A write failed. The tasks printed before it are converted; run the command again. |

Exit status: `0` done (or, with `--check`, would be done); `1` refused or
stopped, read the last line; `2` not started: the lock
`<state_dir>/convergence.lock` is held (a pass or the updater runs), the
directory does not exist, or the arguments are wrong. A refused run changes
nothing. A second run changes nothing.

What to know:

- **The trap.** If step 6 or 7 prints `must-drain`, do not start the new
  dispatcher. It refuses to run anyway: while a task file of the old flow is
  on disk, `python -m dispatcher.main` exits with status 1 and names this
  command. Roll the deploy back, start the old dispatcher, let the named
  tasks finish, and start again at step 3. Step 2 is there to avoid this.
- Every migrated task waits at the plan review gate, also on a track that
  has no plan review.
- A spec session of the old flow that is still live keeps running, and is
  not counted against capacity, until its task gets its turn; the dispatcher
  ends it then. You can close such panes by hand while the dispatcher is
  stopped.
- The console does not show a task at the old gate between step 5 and
  step 7.

After the deploy, delete together: `dispatcher/openspec_migration.py`;
`tests/test_openspec_migration.py` and
`tests/test_openspec_migration_acceptance.py`; `_refuse_old_flow` and its two
calls in `dispatcher/main.py`; the `blocking` parameter of `pass_lock` in
`dispatcher/convergence.py`; the restart-inputs list in step 1 of
`prompts/spec.md` with its tests in `tests/test_prompts.py`; this section.

Deployments may set `AGENT_OPS_COMMAND_WRAPPER` to an executable path that
prepares credentials and then executes its arguments. Without it, sessions call
Podman directly. Use `AGENT_OPS_SESSION_IMAGE` to select your session image.
Never put credentials in a targets file; supply them through your secret manager.

### GitHub permissions and PR polling

The repository PAT needs **Actions: read** to monitor GitHub Actions runs and
**Commit statuses: read** to monitor legacy CI status contexts, in addition to
the existing contents, issues, and pull-request permissions. Dispatching or
rerunning workflows still requires **Actions: write**.

PR lifecycle polling reads merge status and review feedback independently of CI.
CI failures cannot prevent a merged task from moving to Done. For an open PR,
the dispatcher reads Actions runs for its current head SHA and branch, keeping
the latest run/attempt per workflow and event; it also reads the latest commit
status per context. Superseded failures never trigger a repair round.
Unreadable CI sources produce warnings in the dispatcher log; the remaining
sources and PR lifecycle polling continue. Missing CI data is not a green verdict.

Fine-grained PATs cannot call the Checks API. The dispatcher therefore avoids
`statusCheckRollup` and `gh pr checks`. Third-party integrations that publish
only CheckRuns (neither Actions runs nor commit statuses) are not monitored by
this PAT-compatible path. Such integrations require a different authentication
method, such as a GitHub App, to monitor their checks. See GitHub's
[PAT limitations](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens#fine-grained-personal-access-tokens-limitations)
and [Actions API permissions](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-repository).

## License

[MIT](LICENSE) © 2026 jesdi

### Task artifacts

The task page keeps review artifacts accessible across sessions on desktop and
mobile. Open the spec on GitHub beside **Approve plan**, or use **Artifacts**
to revisit prototypes, diagrams, questionnaires, answers and other review files.
Links open the latest published content in a new tab. If GitHub publication
fails, local review and approval remain available.

Sessions register files automatically using the policy in
[prompts/artifacts.md](prompts/artifacts.md). HTML previews are self-contained;
Markdown is committed and pushed to the task branch. On merge, GitHub links
switch to the last verified commit so deleting the branch does not break them.

Stored box copies expire 30 days after done, failed or canceled, on the next
dispatcher pass. Paused tasks never start that countdown; reopening cancels it.
GitHub files and artifact metadata remain, including on archived task pages.
Existing failed-task worktrees are still preserved for autopsy.
