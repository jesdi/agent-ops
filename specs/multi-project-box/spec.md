# Multi-project box — Spec

## Requirements

1. **Box-wide capacity.** `capacity` limits the tasks consuming capacity (as `active()` defines
   them, plus the triage sweep's held unit) summed across **all** targets. Every spawn site
   (claim, wake/resume, feedback) checks this box-wide count.
2. **Optional per-target cap.** A target may set `max_active: N`. A claim for that target is
   refused while N of its own tasks are active, even if box capacity is free. When `max_active`
   is unset, the target has no per-target cap.
3. **In-flight before new.** Each pass first drives, resumes woken tasks, spawns feedback and polls
   PRs for every target. Only then does it claim new work. Woken tasks resume in `updated_at`
   order across all targets.
4. **Fewest-active-first claiming.** Each free unit goes to the target with the fewest active
   tasks that has a claimable candidate and is under its `max_active`. Ties go to the target
   whose most recent `claimed` event in `events.jsonl` is oldest. A target that has never claimed
   counts as oldest.
5. **Optional `setup_cmd`.** An empty or missing `setup_cmd` skips worktree provisioning and does
   not fail the claim.
6. **Optional `verify_cmd`.** With an empty or missing `verify_cmd`, the sessions (review,
   address-review) are told no off-box e2e exists, and they skip that step without claiming e2e
   passed. The PR's GitHub checks are then the only e2e signal, handled by the existing CI loop
   (`loop_caps.ci`).
7. **`gate_cmd` stays required.** A target without `gate_cmd` still fails config load.
8. **Never auto-park specs.** `spec_review_grace_minutes: null` means a spec at
   AWAITING-SPEC-REVIEW never auto-parks. Integer values keep today's meaning, and 0 still means
   park on the next pass.
9. **Project-named Telegram messages.** When more than one target is configured, every per-task
   Telegram message refers to the issue as `{target}#{issue}`. With one target, messages stay
   `#{issue}`.
10. **Per-project operator messages and slot markers.** Operator messages (replies and
    dispatcher notes) and the "waiting for a slot" marker belong to one `(target, issue)`, never to
    a bare issue number. Existing issue-only files are attributed to the single target while only
    one target is configured.

## Scenarios

### Scenario: capacity is shared across targets

- **Given** `capacity: 3`, targets `portfolio_eval` with 2 active tasks and `factorial` with 1
  active task, and both have Ready candidates
- **When** a pass runs
- **Then** no task is claimed for either target

### Scenario: capacity boundary, last unit is used

- **Given** `capacity: 3`, `portfolio_eval` with 2 active tasks, `factorial` with 0 active tasks and
  one Ready candidate
- **When** a pass runs
- **Then** exactly one `factorial` task is claimed and the box has 3 active tasks

### Scenario: a login-parked task still counts box-wide

- **Given** `capacity: 3`, `portfolio_eval` with 2 active tasks and `factorial` with 1 task parked
  for re-login
- **When** a pass runs with Ready candidates on both targets
- **Then** nothing is claimed

### Scenario: a woken task is refused when the box is full

- **Given** `capacity: 3`, 3 active `portfolio_eval` tasks, and a `factorial` task woken after spec
  approval
- **When** a pass runs
- **Then** the `factorial` task stays woken, is marked "capacity full", and does not spawn

### Scenario: woken work goes before new claims across targets

- **Given** `capacity: 3`, 2 active tasks, `portfolio_eval` listed first with Ready candidates,
  and a woken `factorial` task
- **When** a pass runs
- **Then** the `factorial` task resumes and no `portfolio_eval` task is claimed

### Scenario: woken tasks resume in wake order across targets

- **Given** `capacity: 3`, 2 active tasks, a woken `portfolio_eval` task with `updated_at` 10:05,
  and a woken `factorial` task with `updated_at` 10:00
- **When** a pass runs
- **Then** the `factorial` task resumes and the `portfolio_eval` task stays woken ("capacity full")

### Scenario: the free unit goes to the target with fewer active tasks

- **Given** `capacity: 3`, `portfolio_eval` with 2 active tasks, `factorial` with 0, and both with
  Ready candidates
- **When** a pass runs
- **Then** the claimed task belongs to `factorial`

### Scenario: an empty box alternates between targets

- **Given** `capacity: 3`, no active tasks, `portfolio_eval` listed first and last claimed at
  09:00, `factorial` last claimed at 08:00, and both with 3+ Ready candidates
- **When** a pass runs
- **Then** claims go `factorial`, `portfolio_eval`, `factorial`

### Scenario: a target with no candidates yields its share

- **Given** `capacity: 3`, no active tasks, `factorial` with no Ready candidates, and
  `portfolio_eval` with 3 Ready candidates
- **When** a pass runs
- **Then** 3 `portfolio_eval` tasks are claimed

### Scenario: max_active boundary

- **Given** `capacity: 3`, `portfolio_eval` with `max_active: 2` and 1 active task, `factorial`
  with 0 active tasks and no candidates
- **When** a pass runs with 3 Ready `portfolio_eval` candidates
- **Then** exactly one `portfolio_eval` task is claimed, and the box has 2 active tasks with 1
  unit left free

### Scenario: max_active violation is refused

- **Given** `portfolio_eval` with `max_active: 2` and 2 active tasks, and free box capacity
- **When** a pass runs with Ready `portfolio_eval` candidates
- **Then** no `portfolio_eval` task is claimed

### Scenario: empty setup_cmd skips provisioning

- **Given** target `factorial` with no `setup_cmd`
- **When** a `factorial` candidate is claimed
- **Then** the worktree is created, no provisioning container runs, and the spec session spawns

### Scenario: empty verify_cmd never reports e2e as passed

- **Given** target `factorial` with no `verify_cmd`
- **When** its review session runs
- **Then** the rendered prompt states that no off-box e2e run exists for this target, and the
  session runs no `$verify_cmd`

### Scenario: a red GitHub e2e check on the PR drives the CI loop

- **Given** a `factorial` task at PR-OPEN whose PR has a failed `e2e` check
- **When** a pass polls PRs
- **Then** an address-review session spawns and one `ci` round is spent. After `loop_caps.ci`
  (3) rounds the task parks instead.

### Scenario: gate_cmd still required

- **Given** a `targets.yaml` target with no `gate_cmd`
- **When** the config loads
- **Then** loading fails and names the target and the missing `gate_cmd`

### Scenario: null grace never parks

- **Given** `spec_review_grace_minutes: null` and a spec that has waited at AWAITING-SPEC-REVIEW
  for 12 hours
- **When** a pass runs
- **Then** the task is not parked, and its session and capacity unit are kept

### Scenario: zero grace still parks immediately

- **Given** `spec_review_grace_minutes: 0` and a spec that entered AWAITING-SPEC-REVIEW this pass
- **When** the next pass runs
- **Then** the task parks

### Scenario: multi-target Telegram names the project

- **Given** targets `portfolio_eval` and `factorial`, and `factorial` issue 3's spec is ready
- **When** the spec-ready message is sent
- **Then** it starts `📝 factorial#3 `

### Scenario: single-target Telegram is unchanged

- **Given** only target `portfolio_eval`, and issue 412's spec is ready
- **When** the spec-ready message is sent
- **Then** it starts `📝 #412 `

### Scenario: a reply reaches only its own project

- **Given** targets `portfolio_eval` and `factorial`, both with a task for issue 7, and the
  operator replies "use the v2 endpoint" to `factorial#7`
- **When** `portfolio_eval#7` next spawns or resumes
- **Then** its prompt does not contain "use the v2 endpoint", and `factorial#7`'s next prompt
  does, with the message stamped delivered only there

### Scenario: a slot-starved task marks only its own card

- **Given** `capacity: 3` is full, a woken `factorial#12`, and an active `portfolio_eval#12`
- **When** a pass runs
- **Then** the console shows "waiting for a free slot" on `factorial#12` only

### Scenario: legacy files migrate while one target is configured

- **Given** only target `portfolio_eval`, and a legacy issue-only message file for issue 412
  with one undelivered message
- **When** a pass runs and `portfolio_eval#412` next resumes
- **Then** its prompt contains that message

### Scenario: legacy message files are not guessed at with several targets

- **Given** targets `portfolio_eval` and `factorial`, and a legacy issue-only message file for
  issue 3
- **When** a pass runs
- **Then** neither `portfolio_eval#3` nor `factorial#3` receives its messages, and the pass logs a
  warning naming the file

## Out of scope

The console project filter, default ranking without `rank_cmd`, agent-ops as a target,
`target#N` Telegram commands, per-target grace, weighted shares, and auto-approving specs. See
proposal.md.
