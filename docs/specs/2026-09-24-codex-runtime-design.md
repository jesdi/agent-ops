# Codex runtime: stage sessions on OpenAI

## Problem

The router already exists. `models.resolve` launches the first entry of a
track's stage list that the usage gate admits, and `targets.yaml` may already
name `openai/…` entries. They are inert: `usage.admits` fails closed for a
provider with no usage adapter, and every launch path is welded to Claude —
`containers.session_cmd` and `triage_cmd` type `claude …`,
`Sessions.resume` types `--continue`, the Stop hook is a Claude settings
hook, herdr is told `HERDR_AGENT=claude`, and the only mounted home is
claude-home.

So when the Anthropic subscription runs low the box waits, even though the
operator pays for a second subscription it cannot spend. The pace-gate spec
deferred "the Codex usage adapter"; the model-tracks spec deferred "the
OpenAI/Codex runtime". This is both.

## Scope

In scope:

- an `openai` **usage adapter** reading the ChatGPT subscription's windows;
- a **runtime** seam: one record per provider that builds the launch and
  resume commands, so a stage session runs `codex` the same way it runs
  `claude` — interactive, in its herdr tab, parked and resumed by the same
  paths;
- **codex-home**, the Codex counterpart of claude-home, and its seed;
- a **codex-keepalive** unit that keeps the Codex login refreshable;
- per-provider **effort** validation;
- a **git shim** in the session image replacing the Claude-only git
  guardrail hook, for both runtimes;
- the **provider-fixed-per-stage** rule on operator overrides;
- a **gated second model** for Claude review sessions: `review-diff`'s
  `codex exec` correctness pass gets Codex only when the gate admits it.

Out of scope, deferred by name:

- triage on Codex (triage entries must be anthropic; see Config);
- parking a task for a Codex re-login (an expired login fails closed at the
  gate instead);
- cross-runtime continuation: a stage never changes provider, so it never
  needs it;
- a `providers:` kill switch — the track lists already are the switch;
- any provider beyond `anthropic` and `openai`.

## Language

**Runtime**: the CLI a session runs for a provider — `claude` for
`anthropic`, `codex` for `openai`. A record in `dispatcher/runtimes.py`
owns everything that differs between them: home mount, env, herdr agent
name, effort vocabulary, and the launch and resume commands.
_Avoid_: backend, driver, adapter (that word is the usage adapter's)

**Codex-home**: the box-side persistent Codex config directory
(`~/agent-ops-state/codex-home`), mounted at `/root/.codex` inside every
Codex session. Counterpart of claude-home: config, skills, `AGENTS.md`,
transcripts, and the login (`auth.json`).

**Codex-home seed**: its versioned, declarative source in agent-ops-infra
(`provision/codex-home/`), converged by the updater. Credentials and
transcripts are never part of it.

## Design

### Runtimes — `dispatcher/runtimes.py` (new)

Pure data plus two string builders; no I/O.

```python
@dataclass(frozen=True)
class Runtime:
    home: str               # state-dir subdirectory: "claude-home" | "codex-home"
    mount: str              # "/root/.claude" | "/root/.codex"
    env: tuple[str, ...]    # -e flags: CLAUDE_CONFIG_DIR=…, CLAUDE_CODE_OAUTH_TOKEN | CODEX_HOME=…
    herdr_agent: str        # "claude" | "codex"
    binary: str             # host install: ~/.local/bin/claude (mounted :ro at /usr/local/bin/claude) | ~/.local/bin/codex
    package: str            # "" | "/opt/codex": where a packaged CLI's whole package is mounted :ro
    efforts: tuple[str, ...]
    launch: Callable[[str, str, str, str], str]   # (name, worktree, model, effort) -> shell prefix
    resume: Callable[[str], str]                  # (quoted message) -> trailing args

RUNTIMES = {"anthropic": CLAUDE, "openai": CODEX}

def runtime_for(model_id: str) -> Runtime   # KeyError-free: unknown provider raises ValueError
```

Claude, unchanged in behaviour:

```
claude --remote-control <name> --permission-mode auto --model <m> [--effort <e>] <args>
resume: --continue <message>
```

Codex:

```
codex --model <m> [-c model_reasoning_effort=<e>]
      --dangerously-bypass-approvals-and-sandbox
      -c 'notify=["<worktree>/.agent/stop-hook.sh"]'
      -c 'projects={"<worktree>"={trust_level="trusted"}}'
      <args>
resume: resume --last <message>
```

The trust override is an inline table, not a dotted key. Codex's `-c` splits
the key on `.` and keeps quote characters in the segments, so
`projects."<wt>".trust_level` sets a key that never matches the cwd (and a
path with a dot is split apart). Reproduced with codex 0.155.1: the dotted
form still shows the trust prompt, and the inline table skips it. The inline
table replaces any `projects` table in codex-home; the seed has none.

- `notify` fires on `agent-turn-complete`, Codex's equivalent of the Stop
  hook, and runs the same `.agent/stop-hook.sh` the worktree already carries.
  The script ignores its arguments, so Codex's JSON payload is harmless. It is
  set per launch because the worktree path is per task; the seed never knows
  worktrees.
- The trust override pre-empts Codex's first-run trust prompt, which would
  otherwise stall an unattended pane — the same failure `CLAUDE_CONFIG_DIR`
  fixed for Claude.
- `codex resume --last` resumes the newest Codex session for the cwd. The
  worktree is mounted at its host path, and a stage never changes provider
  (see Overrides), so the newest Codex session in that cwd is the stage's.
- `--remote-control` is Claude-only; Codex has no equivalent.

`main.py` never branches on a provider. Everything provider-specific is
reached through `runtime_for(entry.model_id)`.

### Launcher — `dispatcher/containers.py`, `dispatcher/sessions.py`

`session_cmd` keeps its shared part (name, memory, cpus, state-dir wait
mount, worktree and clone mounts, gh and gitconfig mounts,
`AGENT_OPS_TASK_BRANCH`) and takes from the runtime: the home mount
`-v <state_dir>/<home>:<mount>`, its `-e` flags, its host `binary`, and the
command. A Claude session mounts claude-home, plus the Codex binary and
codex-home when it is granted a second model (see below). A Codex session
mounts only codex-home.

`Sessions._launch` sets `HERDR_AGENT` from `runtime.herdr_agent` instead of
the literal `"claude"`. `spawn_stage` and `resume` keep their signatures; the
`model` they already receive selects the runtime.

`triage_cmd` stays Claude-only and asserts an anthropic model.

### Effort — `dispatcher/models.py`

`PROVIDER_EFFORTS` holds each provider's efforts and `parse_entry` validates against the
entry's own provider:

| provider | efforts |
|---|---|
| anthropic | `low medium high xhigh max` |
| openai | `minimal low medium high xhigh` |

`openai/…@max` is a fatal config error, like a typo today. Efforts pass
through verbatim; there is no translation between vocabularies. The sets live
in `models.py` (keyed by provider) so it stays I/O-free and import-light;
`runtimes.py` reads them from there. A provider with no entry in the table is
a config error too.

### Config

- Triage entries must be anthropic: `parse_policy` rejects any other provider
  in `triage:` at load time.
- One new optional key, `models.review_second: openai/<model>`: the model a
  Claude review session's `codex exec` calls run on. Unset means Claude
  sessions never get Codex. It must name a non-anthropic provider that has a
  runtime; anything else is a config error. It must match the `model` in the
  codex-home seed's `config.toml`, because `codex exec` reads its model from
  there. A mismatch misreports only the console label, since OpenAI's windows
  are unscoped and admission is the same for every `openai/…` model.
- Rollout is done by ordering the track lists (see Rollout).

### Second model in Claude review sessions

The review stage runs the `review-diff` skill. Its correctness reviewer runs
`codex exec` when `codex` is on PATH, and otherwise falls back to a subagent on
the session's own model. The skill knows nothing of the gate, so **the mount
is the permission**:

- When a review-stage session launches on an anthropic entry, the dispatcher
  asks the same gate, `admit(models.review_second)`. If admitted, the session
  also gets the host `codex` binary and codex-home mounted, with
  `CODEX_HOME`. If denied or unset, it gets neither, and review-diff takes its
  fallback. The skill is unchanged.
- The decision is made **at spawn**, on the review stage only (not
  address-review), and a resume re-decides it. `bypass_usage` never covers it:
  an operator bypass forces the stage's own pick, not a second subscription.
- Usage needs no bookkeeping of its own. The `codex exec` calls draw on
  OpenAI's real windows, and the next usage fetch sees them.
- A Codex-picked review session already has Codex. Its correctness pass then
  runs on Codex too, with no second model. Fixing that in review-diff (reach
  for `claude -p` when the session is Codex) is out of scope here.
- The decision is a pure function beside `resolve`:
  `second_model(policy, stage, entry, admitted) -> Entry | None`.
  `_spawn_stage` passes the result to `Sessions.spawn_stage` / `resume`, and
  `session_cmd` adds the Codex runtime's mounts. `main.py` still never
  branches on a provider name.

### Usage adapter — `dispatcher/usage_providers.py`

`OpenAIUsage` (name `openai`) is registered in `ADAPTERS`.

- Reads `<state_dir>/codex-home/auth.json` for `tokens.access_token` and
  `tokens.account_id`.
- `GET https://chatgpt.com/backend-api/wham/usage` with
  `Authorization: Bearer <access_token>` and `ChatGPT-Account-Id`.
- Reads `rate_limit.primary_window` and `rate_limit.secondary_window`,
  both unscoped, from `used_percent` and `reset_at` (epoch seconds). The
  kind comes from each window's `limit_window_seconds`, not its slot: a day
  or longer is `WindowKind.WEEKLY`, shorter is `WindowKind.SESSION`. A
  `null` slot is skipped. `limit_reached` (or `allowed: false`) counts every
  window as fully used. Verified on the box 2026-09-24 (ticket 02,
  `tests/fixtures/openai-usage.json`): the `prolite` plan reports only a
  7-day `primary_window` and a `null` `secondary_window`.
- Any failure — missing or unreadable `auth.json`, HTTP error, unparseable
  body, zero windows — returns `unavailable`, and `admits` denies every
  `openai/…` entry. The cache, fan-out and 180 s spacing are the existing
  ones.
- **The adapter never writes credentials.** It does not refresh the token
  (see Keepalive).

### Keepalive — agent-ops-infra

Refresh tokens are single use. infra#10 recorded that two refreshers racing
one lineage invalidated the Claude login every 8–16 h, which is why
`keepalive.sh` is claude-home's only refresher. Codex has no setup-token
equivalent: every Codex session authenticates from, and may refresh,
codex-home's `auth.json`. So the design adds no refresher that isn't Codex
itself.

`codex-keepalive.sh` plus a timer, the same shape as `keepalive.sh`:

1. Read `auth.json`. If there is no refresh token, exit non-zero and let
   `OnFailure` raise the alert ("run `codex login --device-auth` on the box").
2. If the access token is not close to expiry, exit 0.
3. If any session container that has codex-home mounted is running (a
   Codex session, or a Claude review granted a second model), exit 0: Codex
   running there will refresh. The simplest correct test is "no session
   container running at all".
4. Otherwise run
   `CODEX_HOME=<state>/codex-home codex exec "Reply with exactly: ok."`,
   and Codex refreshes through its own code.

Without it a week of Claude headroom lets the Codex token expire unnoticed,
the adapter goes `unavailable`, and the fallback is dead exactly when it is
needed.

### Codex-home seed — agent-ops-infra `provision/codex-home/`

- `config.toml`: `check_for_update_on_startup = false`; no approval or
  sandbox defaults (the launch flags set them); no `notify` (set per launch).
- `AGENTS.md`: the box instructions, the counterpart of the seed's
  `CLAUDE.md`. Both stay authored in the seed; shared text is copied, not
  generated.
- `skills/`: `make vendor-skills` writes the same pinned process skills into
  both `claude-home/skills/` and `codex-home/skills/`. Codex reads
  `SKILL.md` with `name` and `description` frontmatter, which ours carry.
- The sync script converges codex-home the way `claude-home-sync.sh`
  converges claude-home, and never touches `auth.json` or `sessions/`.

Login is done once on the box with `codex login --device-auth` against
codex-home.

### Session image — agent-ops-infra

- No CLI is baked into the image. Since 26d928c the session runs the
  **host's** `claude`, mounted read-only and resolved at spawn, so the box has
  one binary at one version. Codex follows the same pattern:
  `containers._host_claude` becomes the runtime's `binary` mount. Codex is
  a **package**, not one executable: since 0.156 it spawns helpers it finds
  next to its real path (`bin/codex-code-mode-host`, `codex-path/rg`,
  `codex-resources/bwrap`), and without them fails with "failed to spawn
  code-mode host" (verified on the box, 2026-09-25). So the host package
  (the `bin/` parent of `~/.local/bin/codex`'s real path, which must hold
  `codex-package.json`) is mounted read-only at `/opt/codex`, and the image
  links `/usr/local/bin/codex` to `/opt/codex/bin/codex`. The link dangles
  in a session without Codex, so `command -v codex` finds nothing there.
- The infra repo installs the Codex **package** on the host at a pinned
  exact version under `~/.local/lib/codex/<version>`, linked from
  `~/.local/bin/codex`, bumped by hand (Codex has no auto-updater to defer to). The
  codex-keepalive uses the same host binary, so keepalive and sessions never
  disagree about the auth format.
- A **git shim** comes first on `PATH` (`/usr/local/bin/git`). It carries
  the checks from `claude-home/hooks/block-dangerous-git.sh`, including the
  `--force-with-lease origin $AGENT_OPS_TASK_BRANCH` exception and failing
  closed without that variable, then execs the real git. It guards both
  runtimes, and also git run from scripts and Makefiles, which the
  PreToolUse hook never saw.
- The PreToolUse hook and its `settings.json` entry are deleted: one
  guardrail.

### Permission posture

Codex runs with `--dangerously-bypass-approvals-and-sandbox`, and the
container is the sandbox:

- Codex's `workspace-write` sandbox blocks the network that `git push`, `gh`
  and `npm` need.
- Its Landlock/seccomp layer is unreliable in rootless podman.
- Its approval prompts do not fire `notify`, so an approval wait would only
  park through the 600 s stall timer.

Accepted cost: Codex sessions lack Claude auto mode's "risky action → ask the
operator" classifier. What bounds them: the container's mounts (worktree,
clone, read-only gh and gitconfig), the git shim, and GitHub branch
protection on `main`, the backstop for both runtimes.

### Overrides — the provider is fixed for a stage

Switching provider mid-stage throws away the stage's transcript and makes
the new runtime rebuild its context from scratch. That costs tokens for no
gain, and `--continue` / `resume --last` would resume the wrong provider's
newest session, which could be an earlier stage.

Rule: **an override for a stage that already has a pick must name a model of
the pick's provider.** A stage with no pick (a queued claim, or a parked
task whose next launch is a fresh stage) may be overridden to any configured
model.

One helper in `models.py`:

```python
def override_allowed(picks: Mapping[str, str], stage: str, model_id: str) -> bool
```

Enforced at both places that accept an override:

- `web/app.py` `intent_resume` and `_run_model`: 422 with
  "stage <s> runs on <provider>; pick a model from <provider>".
- `main._require_resume_model`, the dispatcher's authoritative check
  when it applies a resume intent. A rejected intent is dropped with an
  event, as an unconfigured model is today.

The console's model picker lists only models the rule allows, so the 422 is
a backstop, not the UX.

Consequence: every `sessions.resume` call site (`_resume_one`, `_retry_plan`,
`_retry_spec`) resumes on the provider that ran the stage, by construction.

### Prompts

Provider-neutral, single source. The one Claude-ism, `plan.md` §3
"Dispatch four reviewer subagents", becomes: "Dispatch four reviewer
subagents if you can dispatch subagents; otherwise run the four reviews
yourself, one after another, each a fresh pass over the spec and the
tickets."

### Console

- Provider windows already render per provider, so OpenAI's session and
  week bars appear when the adapter reports.
- The claude.ai Remote Control link-out is hidden for a session whose pick is
  not anthropic.
- The model picker applies the override rule above.

### CONTEXT.md

Add **Runtime**, **Codex-home** and **Codex-home seed**. Replace "Still
deferred: runtime adapters (running a session on a non-Anthropic provider),
cross-runtime session continuation" with: runtimes ship per provider; a stage
never changes provider, so cross-runtime continuation is excluded by rule, not
pending. Update the Claude-home entry to name its Codex sibling.

## Rollout

Before the deploy that registers the adapter, reorder the box's real
`targets.yaml` so every `openai/…` entry is **last** in its list. Codex is
then picked only when every Anthropic entry of that stage is denied.
Promote entries per track (for example Codex first on review) only after a
Codex stage has run end to end on that track.

The example config's ordering (Codex first on review) is the target state,
not the first deploy.

## Edge cases

- **No Codex login, or an expired one**: the adapter reports `unavailable`,
  `openai/…` is never admitted, and the keepalive alert fires. Nothing parks.
- **Token revoked mid-session**: Codex errors in the pane and goes idle, and
  the stall timer parks it as an ordinary stall. The operator logs in again
  and resumes.
- **Concurrent Codex sessions refresh together** (capacity 2): unverified;
  ticket 1 tests it. If Codex rotates destructively, cap live Codex sessions
  at 1 as an admission rule. That changes a cap, not this design.
- **Codex's bundled ripgrep** (verified on the box, ticket 02): mounting
  only the `codex` executable left `codex-path/rg` absent, and `codex
  doctor` warned "search command could not be verified". Mounting the whole
  package (see Session image) brings the bundled `rg`, so the image needs
  no `ripgrep`.
- **Codex's own sandbox can't start on the host** (verified 2026-09-25):
  `codex exec -s read-only` runs every command through bubblewrap, which
  fails with "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"
  because Ubuntu's AppArmor restricts unprivileged user namespaces
  (`kernel.apparmor_restrict_unprivileged_userns = 1`) for Codex's bundled
  bwrap. Fixed by the system `bubblewrap` package, which Codex prefers on
  PATH: Ubuntu 26.04 already ships the `bwrap-userns-restrict` profile in
  /etc/apparmor.d for `/usr/bin/bwrap` (Codex's docs' extra-profiles step is
  for 24.04). Verified on the box 2026-09-25. Stage sessions are unaffected: they run with
  `--dangerously-bypass-approvals-and-sandbox` inside the container.
  Review-diff's read-only `codex exec` inside a rootless container is
  unverified (ticket 02's rootless-sandbox check).
- **herdr does not recognise `codex`**: `idle_seconds` returns None ("unknown"),
  stall detection is off for Codex panes, and the fix belongs in herdr.
  Ticket 1 verifies it.
- **Review avoids implement's provider**: unchanged. With Codex live, the
  preference now actually takes effect.
- **Second model denied mid-review**: the grant was made at spawn. If the
  OpenAI window runs out during the review, `codex exec` fails. review-diff
  must then fall back as it does when Codex is absent. Verify that it does;
  if its fallback only covers "not on PATH", fix the skill (in
  jesdi/general-skills).
- **A parked pr-open task** resumes as a fresh address-review round, which
  is a fresh spawn. The override rule applies against the `implement` pick,
  since address-review maps to it.

## Testing

- `runtimes`: the launch and resume strings for each provider, including the
  effort flag present or absent, the notify and trust overrides, and no
  `--remote-control` for Codex.
- `containers.session_cmd`: a Codex model mounts only codex-home with
  `CODEX_HOME`, and a Claude model is byte-identical to today.
- `models.parse_entry`: `openai/x@max` rejected, `openai/x@minimal`
  accepted, anthropic unchanged; `triage:` with an `openai/…` entry rejected.
- `override_allowed`: same provider allowed, cross provider denied when a
  pick exists, anything allowed with no pick, address-review checked against
  `implement`.
- Dispatcher: a resume intent across providers is dropped and the task stays
  parked. Web: 422 on both routes.
- `OpenAIUsage`: fixture payload to two windows; missing `auth.json`, 401, and
  an empty body each give `unavailable`. It never opens `auth.json` for writing.
- `second_model`: review + anthropic entry + key set + admitted → the entry;
  denied, key unset, a non-review stage, or a Codex entry → None. Bypass does
  not grant it. `session_cmd` with a grant mounts the codex binary and
  codex-home and sets `CODEX_HOME`, and without one is byte-identical to a
  plain Claude session.
- Session-image git shim: agent-ops-infra `tests/test_box_git_hook.py`
  retargeted from the hook to the shim, same cases.

## Tickets, in order

1. **Verify on the real account** (manual; infra and box): Codex runs in the
   session image with codex-home mounted; herdr reports its agent state; two
   concurrent sessions survive a token refresh; `resume --last` picks the
   right session. Record the findings in this spec. A failure here changes
   the admission cap, not the design.
2. Runtimes seam and launcher (Claude behaviour byte-identical).
3. Per-provider effort, and the triage-provider validation.
4. Override rule: helper, web, dispatcher, console picker.
5. `OpenAIUsage` adapter.
5b. Gated second model for Claude review sessions (`models.review_second`).
6. `plan.md` wording and CONTEXT.md.
7. infra: codex-home seed and sync, vendor-skills to both homes,
   codex-keepalive unit.
8. infra: image with Codex CLI and git shim; delete the PreToolUse hook.
9. Rollout: reorder the box's `targets.yaml`, deploy, and run one task with
   Codex last-resort forced through an override on a fresh stage.
