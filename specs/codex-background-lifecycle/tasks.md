# Bound turns and background results — Tasks

Each ticket begins with isolated locked black-box acceptance tests and ends with `make gate`.
All tickets are sequential because their runtime boundary overlaps. See design.md for contracts.

- [x] T1 — Stale and foreign events cannot park a current turn. Prepare a bound launch; reject old
  turns, foreign conversations, unidentified legacy Codex notify, previous launches and concurrent
  tasks' callbacks. New bound turn/input invalidates old stopped evidence. Normal bound stop alone
  can establish stopped evidence; failed/malformed events cannot.
  Seam: RuntimeControl.prepare/view/event; waitd.handle_ping; main.run_pass.
  Touches: runtime_control.RuntimeControl; waitd.handle_ping; main._drive_task.
  Blocked by: #154 (reuse fix/session-isolation at 558b297 without changing its worktree).
- [x] T2 — Launch-scoped atomic snapshots and native Claude prompt hooks preserve binding/clock.
  Resume gets a fresh launch ID; malformed snapshots yield unknown. Native SessionStart,
  UserPromptSubmit and Stop carry explicit session/prompt identity; late hooks cannot alter current
  turns. Listener is sole writer; stage/ticket bindings and host-only snapshots survive restart.
  Seam: RuntimeControl; waitd HTTP listener; Sessions.spawn_stage/resume; hook subprocess.
  Touches: runtime_control.RuntimeControl.prepare/view/event and native inventory application;
  runtime_snapshots atomic storage/current pointer/schema validation; runtime_http.RuntimeClient
  prepare/view/event and authenticated HTTP dispatch; waitd._Handler.do_POST/_route,
  handle_ping/_native_ping/_native_event/serve; sessions.podman_cmd and
  Sessions._launch/spawn_stage/resume/_resume_context/runtime_view; containers.session_cmd;
  main._launch_stage; workspace.install_stop_hook; hooks/stop-hook.sh.
  Blocked by: T1.
- [x] T3 — Controlled Codex backend and real terminal share one bound conversation. Supervisor
  launches app-server/controller/remote TUI inside task container, with read-only relay/dependency
  mounts and no runtime snapshots mounted. Every session mounts the host wait directory
  read-only, including secondary-provider grants. All container builders reject canonical
  bind overlaps with private runtime state and writable wait-directory aliases before
  returning commands, including unsafe configured layouts and `.git`-selected clone paths.
  Sessions' explicit state directory controls preparation, client paths, provider homes
  and every bind check. Bind sources emit the canonical absolute paths validated;
  container paths remain usable after terminal cwd changes, including on resume.
  Explicit resume/fresh fallback is retained. Service
  death ends session; incompatible required interfaces report unknown/alert. Isolated fake-provider
  integration proves same terminal sees continuation.
  Seam: Sessions.spawn_stage/resume; containers.session_cmd;
  `python3 -P -m dispatcher.codex_supervisor` executable (design.md CLI contract).
  Touches: sessions.podman_cmd; Sessions.__init__/_launch/spawn_stage/resume;
  containers.session_cmd/triage_cmd/setup_cmd/_supervisor_mounts/_session_launch/_runtime_args/
  _host_binary/_bind_mount/_canonical_path/_overlaps/_state_dir;
  codex_supervisor.Controller.__init__/event/problem/notification/lifecycle/bind/start/inventory/recover/connected/observe,
  selected_conversation/recovered_turn/validated_turn/has_initial_input/has_user_message,
  valid_inventory/valid_terminal/backend_command/stop_process/run/main;
  codex_transport.RPC.__init__/call/receive/_packet/_response, connect,
  Gateway.__init__/serve/_downstream/_upstream/_foreign;
  runtime_http.dispatch and BoundClient.__init__/_post/view/event;
  runtime_control._control_unknown/_recover_turn; runtime_snapshots.valid_alerts/valid_snapshot;
  waitd.record_control_session; pyproject websocket dependency.
  Blocked by: T2.
- [x] T4 — Gateway orders operator input against conditional retirement. Accepted input prevents
  old-stop park; revision change/unknown state holds; retired rejects later main input. All terminal
  turn RPCs and controller input traverse the same launch fence. Crash and stage gates remain.
  Seam: RuntimeControl.accept_input/retire; gateway RPC; main.run_pass.
  Touches: runtime_control.RuntimeControl.prepare/event/accept_input/retire/_save,
  _complete_turn/_input_receipt/_retirement_eligible;
  runtime_snapshots.valid_main/valid_snapshot/valid_inputs/valid_input/
  valid_completed_turns/valid_turn_history/valid_current_turn/valid_provenance/
  valid_input_provenance/valid_conversation_provenance;
  runtime_http.RuntimeClient.accept_input/retire, BoundClient.accept_input,
  dispatch/_dispatch_event;
  codex_transport.Gateway.__init__/serve/_upstream/_downstream/_input/_foreign,
  admit_input/input_response/input_receipt;
  codex_supervisor.Controller.start/initial_ack/recover and run/main;
  waitd.handle_ping/_native_ping/_native_identity/_admit_native_prompt/_native_event;
  workspace.install_stop_hook; hooks/stop-hook.sh;
  main._session_evidence/_bound_task_stage/_drive_task/_admit_automatic_park;
  sessions.Sessions.end.
  Blocked by: T3.
- [x] T5 — Owned commands and descendants keep sessions alive with strict cap boundaries.
  Commands use owner thread/initial item and qualify only after surviving matching normal
  main Stop in authoritative inventory; previously qualified workers remain deliverable during
  later active turns. Acceptance includes foreground-output-no-redelivery for commands ending
  before first qualifying Stop. Descendants require validated ancestry at every depth.
  Unknown/missing inventory holds. First stopped report starts clock; only newly reported work
  resets it. Same/subset work, foreground input, partial completion, delivery/reconnect preserve it.
  Exact cap holds, greater-than parks; foreground main is never capped. Nonworking stage gates win.
  Seam: RuntimeControl.event/view/retire; controller inventory; main.run_pass.
  Public contract: runtime-contract.md (typed ownership, qualification, outcomes, checkpoint and clock).
  Touches (implemented): runtime_control.RuntimeControl.retire, _apply_event,
  _native_inventory, _retirement_eligible, _retirement_work, _past_cap;
  runtime_snapshots.valid_snapshot, valid_wait, _supported_workers, valid_work_extensions;
  codex_supervisor.Controller.__init__, notification, lifecycle, start, seed, inventory;
  main._session_evidence, _managed_background_view, _drive_task, _admit_automatic_park;
  machine.ManagedBackgroundView, _in_wait, _wait_actions, _managed_wait_actions, pass_actions.
  New runtime_work module: nonempty, identity_key, valid_identity, valid_node, valid_root,
  valid_link, valid_ancestry, nullable, valid_command_outcome, valid_agent_outcome, valid_outcome,
  valid_command, valid_agent, valid_observation, valid_worker_outcome, matching_stop, valid_stop,
  valid_stored_worker, valid_completion, valid_completions, valid_scope, valid_turn_identity,
  valid_checkpoint, running_workers, inputs_resolved.
  New runtime_inventory module: previously_reported, wait_label, report_wait, merge_checkpoint,
  baseline_worker, qualify, retain_command_identity, retain_previous, merge_worker,
  collect_completions, checkpoint_completions, reconcile_workers, resolved_inventory,
  resolved_worker, report_stopped_work, inventory_alert, apply_inventory.
  New codex_inventory module: require, pages, native_status, native_thread, native_turn, native_item,
  native_terminal, ownership_node, ancestry_for, command_observation, agent_outcome,
  agent_observation, NativeInventory.__init__, NativeInventory.remember_item,
  NativeInventory.remember_end, NativeInventory.notification, NativeInventory.discover,
  NativeInventory.read_thread, NativeInventory.loaded_history, NativeInventory.remember_turns,
  NativeInventory.remember_entries, NativeInventory.history,
  NativeInventory.commands, NativeInventory.partial, NativeInventory.ended_agents,
  NativeInventory.owning_stops,
  NativeInventory.owner_inventory, NativeInventory.scan, incomplete_turn_history, missing_current_turn,
  turn_items, initial_checkpoint,
  scan_checkpoint, stored_commands, unique_identities.
  Review fix round 3: codex_inventory.NativeInventory.remember_item, discover, history,
  ended_agents; incomplete_turn_history, missing_current_turn. Required failed-turn history
  recovers or holds unknown, continuing notLoaded holds without losing definite failures,
  validated discovery metadata survives an unreadable first history read, and every initial
  command cache update rejects a contradictory initiating turn.
  Review fix round 4: codex_inventory.NativeInventory.history. Every exact owner history
  requires complete opaque-paged thread/turns/list evidence, including launch seeding;
  reduced full reads cannot hide later same-status or no-item outcomes. Unreadable turn
  pages hold unknown while preserving definite older outcomes and the established baseline.
  A still-unloaded owner retains readable older terminal outcomes and exact item messages
  before rejecting its current history as unknown; it supplies no main Stop.
  Failed item-page queries still retain independently readable full-turn outcomes and
  messages before reporting unknown; both required query scopes must succeed for known inventory.
  CRAP fix helpers: codex_supervisor.main_lifecycle_turn;
  runtime_snapshots.valid_reported_identities.
  CRAP regression seam: tests/test_t5_crap_regressions.py (RuntimeControl and run_pass).
  Regression seams: tests/test_t5_inventory_regressions.py, test_t5_review_regressions.py,
  and t5_observed_session.py
  (public supervisor main with real external listener/native provider); update the historical
  test_runtime_persistence reappearing-subset expectation to the approved cumulative clock.
  Merged after independent review and a green full make gate. One same-scan result-fidelity
  defect remains open under T6: an available message in the first full-turn response can be
  lost when an earlier item query succeeded. Final reviewers recorded APPROVE and REQUEST
  CHANGES (Important/P2); the round-four cap ruling accepted merge with that finding retained
  as a T6 obligation. No delivery transmission.
- [ ] T6 — Successful and failed outcomes promptly reach idle/active main turns. Batch available
  outcomes without waiting for other workers. Preserve command output/exit and child errors.
  Active delivery uses exact-turn precondition; idle uses same-conversation continuation. Confirmed
  races re-read state without duplicate turns. Pending outcomes hold input park. Native child
  notifications do not create duplicate controller deliveries.
  Seam: gateway delivery; RuntimeControl delivery records; fake-provider session flow.
  Touches: controller delivery; gateway; runtime_control receipt operations.
  Blocked by: T5.
- [ ] T7 — Disconnect/restart/lost acknowledgments/duplicates preserve durable receipt state.
  Persist sent-unconfirmed before send; history client-message match confirms acceptance. Unknown
  receipt remains pending, alerts once, never blindly resends. Recover missed outcomes and owned
  work/history from bound conversation; reconnect preserves clock and live terminal. Missing
  inventory never invents success; confirmed rejection differs from uncertainty.
  Seam: controller reconnect/history; RuntimeControl delivery persistence; fake provider.
  Touches: controller reconciliation; runtime_control receipts; gateway transport.
  Blocked by: T6.
- [ ] T8 — Alerts resolve on receipt; real herdr/Podman launches preserve isolation/attachment.
  Emit task-scoped events and existing operator notifications, once per pending problem; confirmed
  receipt resolves condition. Verify two concurrent tasks, replaced tickets, actual descendants and
  child errors, Claude native payloads, container mounts/real terminal. Complete final branch review
  and review-ready PR; deployment/live task resumes are excluded.
  Seam: event/history/operator surfaces; actual Sessions launch; isolated fake-provider E2E.
  Touches: runtime_control alerts; dispatcher alert presentation; runtime acceptance scripts/docs.
  Blocked by: T7.
