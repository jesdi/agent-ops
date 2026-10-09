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
  Merged after independent review and a green full make gate. The final capped review recorded
  APPROVE and REQUEST CHANGES (Important/P2) for losing an available same-scan message after
  a successful empty item query. The round-four cap ruling accepted merge with that specific
  result-fidelity defect carried under T6; T6 now preserves the exact-turn message before first
  completion publication. T5 did not add delivery transmission.
- [x] T6 — Successful and failed outcomes promptly reach idle/active main turns. Batch available
  outcomes without waiting for other workers. Preserve command output/exit and child errors.
  Active delivery uses exact-turn precondition; idle uses same-conversation continuation. Confirmed
  races re-read state without duplicate turns. Pending outcomes hold input park. Native child
  notifications do not create duplicate controller deliveries.
  Seam: gateway delivery; RuntimeControl delivery records; fake-provider session flow.
  Public contract: runtime-delivery-contract.md and t6-public/ declarations.
  Acceptance also preserves safe available exact-turn messages exposed by full-turn history
  before first completion publication despite an earlier successful empty item-page response.
  Touches (implemented; exact function inventory):
  - `dispatcher/runtime_control.py`: `_recover_turn`, `RuntimeControl.event`, `RuntimeControl._apply_current_event`, `_retirement_eligible`.
  - `dispatcher/runtime_snapshots.py`: `valid_snapshot`.
  - `dispatcher/codex_inventory.py`: `NativeInventory.history`, `merge_history_entries`,
    `NativeInventory.discover`, `NativeInventory.read_thread`, `NativeInventory.loaded_history`.
    Review round 2 validates inherited native thread reply boundaries so malformed replies
    preserve unknown inventory and continued observation instead of stopping the observer.
  - `dispatcher/codex_supervisor.py`: `Controller.__init__`, `Controller.connected`, `run`.
  - `dispatcher/codex_transport.py`: `RPC.begin`, `RPC.finish`, `RPC.call`.
  - `dispatcher/runtime_delivery.py`: `unassigned`, `results_resolved`, `task_matches`, `gate_open`, `valid_members`, `valid_text_item`, `payload_records`, `matches_records`, `batch_records`, `propose`, `membership_matches`, `current_revision`, `find_batch`, `send_selection`, `same_attempt`, `send`, `send_available`, `known_rejection`, `mismatched_turn_message`, `receipt_for`, `resolve`, `acknowledge`, `reject`, `apply_delivery`.
  - `dispatcher/runtime_delivery_schema.py`: `bounded_revision`, `valid_deliveries`, `unique`, `unique_assignments`, `valid_batch`, `valid_batch_attempts`, `valid_attempt`, `valid_target`, `valid_attempt_input`, `valid_pending`, `valid_confirmation`.
  - `dispatcher/codex_delivery.py`: `selection`, `active_selection`, `response_event`, `accepted_turn`, `DeliveryPump.__init__`, `DeliveryPump.view`, `DeliveryPump.event`, `DeliveryPump.poll`, `DeliveryPump.propose`, `DeliveryPump.forward`, `DeliveryPump.receipt`, `DeliveryPump.close`.
  - `tests/test_t6_author_delivery.py`: public listener contradictory-rejection regression;
    `lose_sent_response` owned Unix HTTP proxy and `ListenerResponseLossTests` executable
    durable-reservation/response-loss regression.
  - `tests/test_t6_crap_regressions.py`: public runtime replay/input/storage validation and
    observed supervisor delivery selection, uncertainty, independent progress and durability regressions.
  - `tests/test_t6_review_recovery.py`, `tests/t6_review_wire.py`, `tests/t6_review_view.py`:
    executable-boundary malformed root-result recovery and postproposal launch-view
    uncertainty checks using owned wire faults, with retained receipts and later delivery.
  - `tests/test_t6_inventory_recovery.py`, `tests/t6_inventory_wire.py`:
    executable-boundary null/list/scalar replies to exact history read, retained-owner read,
    and notLoaded resume preserve pending state and clocks, expose inventory uncertainty,
    keep operator input live, and recover polling and later exact-root outcome delivery.
  - `tests/t6_locked/test_external_delivery.py::test_supplementary_child_completion_does_not_repeat_main_submission`:
    supplementary replay barrier.
  - `tests/test_bound_turns_t5_executable_acceptance.py::test_unreadable_native_baseline_before_bootstrap_cannot_be_seeded_as_empty`:
    H02 positive compatibility publication wait.
- [ ] T7 — Disconnect/restart/lost acknowledgments/duplicates preserve durable exact receipt state and controller ownership.

  New T7-managed Codex preparation records explicit unattempted bootstrap provenance; retained absent provenance remains unknown. Persist immutable exact root operation and ordinary initial input before forwarding; unknown results and replay never authorize another root or prompt. Optional existing ordinary-input provenance preserves two-argument/Claude compatibility and richer native input access. Confirm only exact immutable root/client/item/turn matches after complete scoped native item paging. Receipt-local full normal-own-end proof settles its own input without fabricating current main Stop or altering wait/session/work authority. Positive current native idle selection can route a fresh gated turn/start while retained stale lifecycle state remains unchanged, subject to all existing input barriers. Known T6 rejection remains distinct from uncertainty. Record at most one durable uncertainty condition per logical batch and resolve/reopen it through exact existing receipt rules; T8 owns operator/audit presentation.

  Expose the declared production controller ownership context and use it in `run()`. A fresh default attachment uses durable exact-current state, never bootstraps/reseeds/revives, and can reconcile retained receipts. First-launch uses only explicit durable permission. A fully closes its owned observer/callback/listener/receipt activity before B enters with the same live backend/Gateway/TUI/shared lock. Ready requires legitimate same-launch initial acceptance, not invented completion. Same-launch recorded service death is terminal for later live/unknown events and both attachment modes; a separately authorized new physical launch remains allowed. Preserve T4/T5/T6 gates, clocks, identities, baseline, receipts, forced closure, runtime compatibility, Sessions mounts/environment and ordinary native command shapes.

  Seam: exact-launch `RuntimeControl`/`RuntimeClient`/`BoundClient` public view/event/input; native exact-root item/turn pages and current thread observation; declared `dispatcher.codex_supervisor.attach_controller` context/handles reused by production; unchanged public Gateway and its shared input lock; owned fake-provider wire faults for later independent acceptance. No new state-mutating callable, endpoint, retry policy, presentation interface or CLI interpreter takeover.

  Public contract: runtime-receipt-contract.md and t7-public/ declarations.

  Touches (implemented; exact function inventory):

  - `dispatcher/runtime_control.py`: `RuntimeControl.prepare`, `accept_input`, `_apply_current_event`; `_bind`, `_service`, `_input_receipt`, `_retirement_eligible`, `_current_stop_follows_inputs`, `_replayable_event`, `_valid_input_provenance`; existing event dispatch table. Bootstrap and immutable admission provenance, terminal service death, receipt events and current-Stop retirement fence.
  - `dispatcher/runtime_bootstrap.py` (new): `initial_text`, `root_target`, `valid_bootstrap`, `valid_root_binding`, `attempt_root`, `fresh_revision`, `bind_root`, `send_initial`. Durable one-shot root and initial-input permission.
  - `dispatcher/runtime_input.py` (new): `valid_span`, `native_item`, `native_attachment`, `native_target`, `valid_native_input`, `text_input`, `supported_text`, `same_text`, `input_sources`, `matches_input`. Ordinary native packet validation and narrow exact text recovery semantics.
  - `dispatcher/runtime_history.py` (new): `valid_scan`, `matching_receipt`, `same_identity`, `accept_history`, `confirm_batch`, `normal_end_matches`, `settle_history`, `valid_extensions`, `valid_history`, `valid_settlement`. Exact receipt and receipt-local normal-end proof, separate from lifecycle authority.
  - `dispatcher/runtime_snapshots.py`: `valid_alerts`, `valid_alert`, `valid_snapshot`, `valid_provenance`, `valid_input_provenance`. Validate optional durable records and one uncertainty condition per batch.
  - `dispatcher/runtime_http.py`: `RuntimeClient.accept_input`, `BoundClient.accept_input`. Carry optional native provenance over the existing input route.
  - `dispatcher/runtime_delivery.py`: `send_selection`, `start_selection`, `resolve`, `reject_uncertain`, `acknowledge`, `resolve_condition`, `uncertain`, `apply_delivery`; `dispatcher/runtime_delivery_schema.py`: `valid_confirmation`. Fresh native-idle admission, compatible ACK/history confirmation and exact-attempt uncertainty resolution/reopening.
  - `dispatcher/codex_transport.py`: `Gateway._input`, `admit_input`. Admit the final ordinary native packet under the existing shared input lock.
  - `dispatcher/codex_ownership.py` (new): `listener_call`. Await real listener worker completion when its controller invocation is cancelled.
  - `dispatcher/codex_receipts.py` (new): `scan_scope`, `unique_message`, `receipt_event`, `settlement_event`, `reconcile`, `reconcile_receipt`. Independent exhausted native item scan and full normal-own-end observation.
  - `dispatcher/codex_supervisor.py`: `Controller.__init__`, `view`, `event`, `problem`, `lifecycle`, `root_params`, `bind` (including `received`), `start`, `submit_initial`, `initial_reply`, `initial_ack`, `seed`, `inventory`, `recover`, `receipts`, `connected`, `finish_initial`, `observe`, `close`; new `ControllerAttachment.__init__`, `observe`, `wait_ready`, `wait_closed`, `attach_controller`; `run`; removed `has_initial_input`, `has_user_message`. Durable first-launch/default recovery and quiescent ownership reused by the production CLI; readiness requires durable initial acceptance. Transient listener transport loss in initial observation or diagnostic publication retains the observer; invalid current binding and cancellation retain their terminal ownership semantics.
  - `dispatcher/codex_delivery.py`: `DeliveryPump.view`, `event`, `poll`, `poll_batch`, `forward`, `receipt`, `uncertain`, `close`. Capture listener revision before native selection and independently own pending receipts.
  - `dispatcher/codex_inventory.py`: `NativeInventory.remember_entries`, `history`. Reuse existing canonical ownership/cache validators for full command-startup history after offline descendant outcomes; preserve baseline, child qualification and root Stop authority without paging changes.
  - `tests/test_t7_author_receipts.py` (new): `test_ordinary_steer_ack_cannot_change_the_durably_selected_turn`, `test_history_settlement_does_not_revalidate_stop_that_precedes_new_input`, `test_malformed_native_item_discriminant_rejects_without_partial_admission`, `test_old_rejection_replay_cannot_resolve_later_attempt_uncertainty`; `tests/test_t7_author_attachment.py` (new): `HeldListenerResponse.__init__`, `start`, `serve`, `close`, `AttachmentListenerQuiescenceTests.test_bound_root_with_no_input_admission_can_finish_first_launch_once`, `test_exit_awaits_inflight_listener_response_while_gateway_remains_usable`. Public behavioral regressions and real wire quiescence proof.
  - `tests/test_t6_crap_regressions.py`: `test_uncertain_ack_preserves_pending_batch_while_operator_and_new_batch_progress`; `tests/test_t6_review_recovery.py`: `test_postproposal_view_uncertainty_preserves_batch_and_recovers`. Preserve their uncertainty and input barriers with public wire faults/admissions under T7 recovery.
  - `tests/codex_control_fixture.py`: `Socket.send`; `tests/test_codex_supervisor.py`: `test_accepted_initial_turn_attaches_terminal_despite_lost_ack`. Retain the accepted initial item in real provider history while dropping its ACK, and require exact durable receipt acceptance before terminal readiness.
  - `tests/test_t7_crap_inputs.py` (new): `test_native_content_is_retained_and_acknowledged_without_normalization`, `test_malformed_native_item_does_not_consume_input_identity`, `test_malformed_native_target_does_not_partially_admit`, `test_invalid_ack_preserves_pending_receipt_for_later_normal_ack`, `test_dead_service_replay_is_stable_and_cannot_admit_or_revive`, `test_native_provenance_cannot_upgrade_existing_or_other_runtime_input`. Public ordinary-input validation, immutable ACK provenance, and terminal-service replay cases.
  - `tests/test_t7_crap_delivery.py` (new): `contract`, `test_start_needs_exact_idle_selection_when_main_has_no_current_stop`, `test_known_rejection_resolves_only_its_uncertainty_and_retry_remains_separate`, `test_corrupt_confirmation_is_unknown_and_cannot_mutate_until_exact_storage_restored`. Public delivery admission/rejection and corrupt-storage reads.
  - `tests/test_t7_crap_recovery.py` (new): `test_completed_history_needs_the_same_item_before_receipt_local_settlement`, `test_uncertain_delivery_repolls_without_resend_then_history_confirms_same_attempt`. Real supervisor recovery through independent native/listener sockets; preserve exact attempts and separate receipt settlement from lifecycle evidence.
  - `tests/test_t7_listener_reconnect.py` (new): `ListenerOutage.__init__`, `start`, `serve`, `close`; `ListenerReconnectTests.recovery_after_outage` (including `accepted`), `test_initial_observer_view_outage_recovers_same_delivery`, `test_connected_view_and_diagnostic_outage_recovers_same_delivery`. Real BoundClient Unix HTTP outages followed by exact pending-delivery history recovery on the same public attachment, backend, Gateway lock and launch; no resend or lifecycle/clock fabrication.
  - `specs/codex-background-lifecycle/tasks.md`: exact implemented scope only; isolated acceptance corrections are separately authorized and locked by their original author.

  Blocked by: T6.
- [ ] T8 — Alerts resolve on receipt; real herdr/Podman launches preserve isolation/attachment.
  Emit task-scoped events and existing operator notifications, once per pending problem; confirmed
  receipt resolves condition. Verify two concurrent tasks, replaced tickets, actual descendants and
  child errors, Claude native payloads, container mounts/real terminal. Complete final branch review
  and review-ready PR; deployment/live task resumes are excluded.
  Seam: event/history/operator surfaces; actual Sessions launch; isolated fake-provider E2E.
  Touches: runtime_control alerts; dispatcher alert presentation; runtime acceptance scripts/docs.
  Blocked by: T7.
