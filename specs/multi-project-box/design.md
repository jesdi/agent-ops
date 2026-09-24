# Multi-project box — Design

## Decisions

- **One box-wide occupancy count, `_box_free(cfg, tasks)`, checked at every spawn site** (claim,
  wake/resume, feedback). It uses `active()` over all tasks and `eff.capacity`, which already
  subtracts the triage sweep's held unit. Tradeoff: a busy target can use capacity another target
  would have wanted. Fairness applies only to claims (next decision), never to in-flight work.
- **The pass splits into two box-wide phases.** Phase 1 runs over every target: drive, `_wake_ci`,
  `_poll_prs`. It then resumes woken tasks for all targets, in `updated_at` order, and after that
  spawns feedback for all targets, in `updated_at` order. Phase 2 claims. Tradeoff: one
  more loop over the targets per pass. In exchange, an approved spec never waits behind another
  target's new claim.
- **Claims pick fewest-active-first.** For each free unit, the target with the fewest active
  tasks wins, among targets that still have a candidate and are under `max_active`. Ties go to the
  oldest `claimed` event in `events.jsonl`, and a target that has never claimed counts as oldest.
  Each target's candidate generator is created lazily and pulled one candidate at a time, so
  `rank_cmd` runs only for targets that get a turn. Tradeoff: reading the event log every pass,
  which is cheap because `read_tail` already reads it whole. Rotation can lose history, and then a
  tie falls to "never claimed" (list order), which is harmless.
- **A target's provisioning failure removes it from the claim round for this pass.** This keeps
  today's cap of one failure per target per pass, and other targets keep claiming.
- **`max_active` applies to claims only.** Woken work and feedback for a target at its cap still
  run: that work is already owned, and refusing it would strand approved specs.
- **`setup_cmd` and `verify_cmd` default to `""`.** An empty `setup_cmd` skips the setup
  container in `create_workspace`. An empty `verify_cmd` swaps the e2e sections of `review.md` and
  `address_review.md` for a "no pre-PR e2e" fragment. Tradeoff: more prompt fragments, but a
  session never sees a blank command it could misread as passed.
- **The e2e sections move into fragments**: `prompts/e2e_review.md`,
  `prompts/e2e_address_review.md` and `prompts/no_e2e.md`. `_spawn_stage` renders the right
  fragment into `$e2e_step`. The PR-body "Verification" line refers to the fragment's result
  instead of hardcoding "green e2e run URL".
- **`spec_review_grace_minutes` is `int | None`, and `None` means never.** `_grace_elapsed`
  returns False for `None`. Integers, including 0, keep their meaning. Tradeoff: specs waiting
  for review hold capacity, which the operator accepted.
- **Telegram names the project through `ref`.** `Notifier(multi_target=...)` is built from
  `len(cfg.targets) > 1`. `send()` sets `ref` to `{target}#{issue}` when multi-target and a
  target is given, otherwise to `#{issue}`. Per-task templates use `{ref}` in place of
  `#{issue}`. Every per-task `notifier.send` passes `target`.
- **Forced by red-team: operator messages and wake-blocked markers are keyed by target.**
  `messages/{target}-{issue}.jsonl` and `wake-blocked-{target}-{issue}` replace the issue-only
  keys, closing the two "deferred rekey" gaps noted in `web/app.py` and `web/sources.py`. With two
  targets, a reply for `factorial#7` would otherwise be delivered into `portfolio_eval#7`'s next
  prompt and stamped delivered.
- **Legacy key migration runs at pass start, and only while one target is configured.** Legacy
  `messages/{issue}.jsonl` and `wake-blocked-{issue}` are renamed to that single target's key.
  With more targets, legacy wake-blocked markers are deleted, since they are transient and get
  re-marked next pass. Legacy message files are left in place with a stderr warning, because they
  cannot be attributed. The runbook order enforces this: deploy, let one pass run, then add the
  second target.

## Data model

There is no database. The "tables" are `targets.yaml` fields, which `load_config` validates at
load time, and on-disk keys under `state_dir`. Every invariant below is enforced at load or at the
one write site, never left to callers.

| Table | Field | Constraint | Why |
|---|---|---|---|
| `targets.yaml` (root) | `capacity` | int ≥ 1, required semantics now box-wide | The only concurrency limit; 3 on the 8 GB box. |
| `targets.yaml` (root) | `spec_review_grace_minutes` | `null` or int ≥ 0; missing = 15 | `null` = never auto-park; 0 keeps "park next pass". |
| `targets.yaml` target | `gate_cmd` | non-empty string, load fails naming the target | Only guard against unattended broken code. |
| `targets.yaml` target | `setup_cmd` | string, default `""` | `""` skips provisioning. |
| `targets.yaml` target | `verify_cmd` | string, default `""` | `""` means no pre-PR e2e; PR checks are the signal. |
| `targets.yaml` target | `max_active` | absent, or int with 1 ≤ n ≤ `capacity`; load fails otherwise | A cap above capacity is meaningless; 0 would silently disable a target. |
| `targets.yaml` target | `name` | unique across targets (existing) | Every on-disk key below embeds it. |
| state_dir | `messages/{target}-{issue}.jsonl` | single writer: dispatcher `_queue_message` | Stops operator mail crossing projects. |
| state_dir | `wake-blocked-{target}-{issue}` | single writer: `_mark_wake_blocked` | The badge names the right card. |
| state_dir | `events.jsonl` `claimed` rows | carry `target` (existing) | Source of the least-recently-claimed tiebreak. |

## Seams

- `dispatcher.main.run_pass(cfg, deps)`: **existing**, and the main seam. It drives capacity,
  ordering, fairness, `max_active`, grace, the prompt `e2e_step`, message delivery and
  wake-blocked markers. It needs `FakeSessions` / `FakeNotifier` in `tests/test_main.py` to record
  `target` alongside the issue (a small, existing-fake change).
- `dispatcher.config.load_config(path)`: **existing**. Optional fields, `max_active` bounds,
  `null` grace, required `gate_cmd`.
- `dispatcher.workspace.create_workspace(target, issue)`: **existing**. An empty `setup_cmd` runs
  no setup container.
- `telegram.notify.Notifier(multi_target=...).send(template, **ctx)` in dry-run mode, and
  `telegram.templates.render`: **existing**, with a new `multi_target` constructor argument.
- `dispatcher.messages` (`append` / `undelivered` / `mark_delivered` / `all_messages`, now taking
  `target`) and `web.sources.Sources.messages(target, issue)` /
  `undelivered_counts()` / `wake_blocked_issues()`: **existing**, with changed signatures.
