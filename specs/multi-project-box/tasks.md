# Multi-project box — Tasks

Each task links to the goal in proposal.md: two or more targets share the box safely and fairly
under one box-wide `capacity`, in-flight work goes first, and a repo onboards without e2e or
provisioning scripts. Work the frontier (tasks whose blockers are done), commit per task with its
ID, and finish every task with `make test` green. Tests come first in every task.

- [ ] **T1**: Config accepts the new shape: `setup_cmd` / `verify_cmd` optional (default `""`),
  `max_active` (absent or 1..capacity, otherwise load fails), `spec_review_grace_minutes: null`
  (int ≥ 0 otherwise). Missing `gate_cmd` still fails, naming the target. Document all of it in
  `targets.example.yaml`. _Goal: a factorial target can be declared in a few lines with no fake
  commands._
  Seam: `dispatcher.config.load_config(path)`. Blocked by: none.
- [ ] **T2**: `null` grace never parks: a spec waiting 12 h at AWAITING-SPEC-REVIEW keeps its
  session and capacity unit, and 0 still parks on the next pass. _Goal: specs wait for the
  operator during the test instead of parking._
  Seam: `dispatcher.main.run_pass(cfg, deps)`. Blocked by: T1.
- [ ] **T3**: Box-wide capacity at every spawn site. Claim, wake/resume and feedback all count
  `active()` over every target (login-parked included) against `eff.capacity`. Extend
  `FakeSessions`/`FakeNotifier` to record `target`. Covers the scenarios "capacity is shared",
  "last unit is used", "login-parked counts" and "woken task refused when full". _Goal: two
  targets can never exceed 3 sessions and OOM the box._
  Seam: `dispatcher.main.run_pass(cfg, deps)`. Blocked by: none.
- [ ] **T4**: Two-phase pass. Phase 1 drives every target, then resumes woken tasks box-wide in
  `updated_at` order, then spawns feedback box-wide. Phase 2 claims. Covers "woken work before new
  claims" and "wake order across targets". _Goal: an approved factorial spec never waits behind
  new portfolio_eval claims._
  Seam: `dispatcher.main.run_pass(cfg, deps)`. Blocked by: T3.
- [ ] **T5**: Fewest-active-first claiming. Lazy per-target candidate generators, the
  least-recently-`claimed` tiebreak from `events.jsonl`, `max_active` refusal, an empty target
  yielding its share, and a provisioning failure removing only that target for the pass. Covers
  "fewer active wins", "empty box alternates", "empty queue yields", and the `max_active` boundary
  and violation. _Goal: both projects progress when both have Ready work._
  Seam: `dispatcher.main.run_pass(cfg, deps)`. Blocked by: T1, T4.
- [ ] **T6**: An empty `setup_cmd` skips provisioning. The worktree is created, no setup container
  runs, and the spec session spawns. _Goal: factorial needs no provisioning script._
  Seam: `dispatcher.workspace.create_workspace(target, issue)`. Blocked by: T1.
- [ ] **T7**: An empty `verify_cmd` renders the no-e2e fragment. Move the e2e sections of
  `review.md` / `address_review.md` into `prompts/e2e_review.md` / `prompts/e2e_address_review.md`,
  add `prompts/no_e2e.md`, and have `_spawn_stage` fill `$e2e_step`. A factorial review prompt
  says there is no pre-PR e2e and contains no `$verify_cmd` step. A red `e2e` PR check still
  drives address-review under `loop_caps.ci`. _Goal: factorial's e2e lives in GitHub checks
  without the session reporting a fake pass._
  Seam: `dispatcher.main.run_pass(cfg, deps)` (the prompt FakeSessions captures). Blocked by: T1.
- [ ] **T8**: Key operator messages and wake-blocked markers by target
  (`messages/{target}-{issue}.jsonl`, `wake-blocked-{target}-{issue}`). Add the single-target
  legacy migration at pass start, and have the web read the new keys (`Sources.messages(target,
  issue)`, `undelivered_counts()` / `wake_blocked_issues()` returning `(target, issue)`). Drop
  the two "deferred rekey" comments. A reply to `factorial#7` is never drained by
  `portfolio_eval#7`. _Goal: two projects with overlapping issue numbers never cross mail or
  badges._
  Seam: `dispatcher.main.run_pass(cfg, deps)` and `web.sources.Sources`. Blocked by: none.
- [ ] **T9**: Telegram names the project. `Notifier(multi_target=len(cfg.targets) > 1)`, the
  `{ref}` placeholder in every per-task template, and `target` passed by every per-task send.
  `📝 factorial#3 ` with two targets, `📝 #412 ` with one. _Goal: the operator knows which
  project a ping is about._
  Seam: `telegram.notify.Notifier(dry_run=True, multi_target=...).send(...)`. Blocked by: none.
