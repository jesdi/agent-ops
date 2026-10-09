# Static host criterion map

Names are independently selectable through the standalone runner. The actual
baseline and cleanup evidence are recorded in `baseline-record.json`.

98 independently named cases cover the unchanged 42 H criterion groups.
Actual host baseline: 87 behavioral RED, 11 H42 existing-policy GREEN, zero ERROR.
All 18 N cases remain UNRUN in `native-evidence-checklist.md`.

| Criterion | Independently selectable locked cases |
| --- | --- |
| H01 | `test_H01_pending_claim_audit_notice_preserves_pending_hold` |
| H02 | `test_H02_compatibility_before_delivery_observations_stay_independent`<br>`test_H02_delivery_before_compatibility_observations_stay_independent`<br>`test_H02_repeated_checks_and_changed_diagnostic_keep_one_claim` |
| H03 | `test_H03_controller_attachment_replacement_keeps_claim_identity` |
| H04 | `test_H04_new_presenter_process_does_not_reuse_durable_claim` |
| H05 | `test_H05_listener_restart_preserves_claim_without_repeat` |
| H06 | `test_H06_lost_real_claim_reply_does_not_authorize_effect_or_retry` |
| H07 | `test_H07_crash_after_durable_claim_before_effect_is_not_retried` |
| H08 | `test_H08_real_log_append_failure_never_reopens_presentation` |
| H09 | `test_H09_zero_notifier_return_is_not_receipt_or_retry_authority` |
| H10 | `test_H10_notifier_timeout_retains_hold_and_never_retries` |
| H11 | `test_H11_ack_resolves_only_its_batch_without_resolution_push` |
| H12 | `test_H12_history_resolves_only_its_batch_without_resolution_push` |
| H13 | `test_H13_duplicate_ack_never_repeats_confirmation_or_effect`<br>`test_H13_duplicate_history_never_repeats_confirmation_or_effect`<br>`test_H13_foreign_history_cannot_confirm_batch_or_create_another_main_turn`<br>`test_H13_stale_ack_does_not_resolve_current_batch` |
| H14 | `test_H14_known_rejection_cannot_claim_or_announce_delivery` |
| H15 | `test_H15_safe_reopened_uncertainty_reuses_condition_and_initial_claim` |
| H16 | `test_H16_two_batches_get_independent_pending_claims` |
| H17 | `test_H17_identical_batch_string_is_independent_across_tasks` |
| H18 | `test_H18_fresh_physical_launch_rejects_old_callbacks` |
| H19 | `test_H19_new_implement_ticket_with_same_conversation_has_new_identity` |
| H20 | `test_H20_current_task_park_suppresses_claim_and_effect`<br>`test_H20_current_task_stage_suppresses_claim_and_effect`<br>`test_H20_current_task_ticket_suppresses_claim_and_effect`<br>`test_H20_current_task_worktree_suppresses_claim_and_effect` |
| H21 | `test_H21_forced_retirement_before_claim_suppresses_presentation_and_claim`<br>`test_H21_missing_current_association_cannot_use_retained_launch_as_authority`<br>`test_H21_replacement_before_first_claim_cannot_present_retained_old_condition` |
| H22 | `test_H22_known_park_after_claim_before_effect_consumes_claim_without_effect`<br>`test_H22_known_replacement_after_claim_suppresses_both_effects`<br>`test_H22_known_retirement_after_claim_suppresses_both_effects`<br>`test_H22_known_stage_after_claim_suppresses_both_effects`<br>`test_H22_known_ticket_after_claim_suppresses_both_effects`<br>`test_H22_known_worktree_after_claim_suppresses_both_effects` |
| H23 | `test_H23_known_park_during_real_audit_open_suppresses_later_push` |
| H24 | `test_H24_own_receipt_between_claim_and_effect_suppresses_stale_pending_push` |
| H25 | `test_H25_replacement_racing_after_final_check_never_relabels_context` |
| H26 | `test_H26_compatibility_unknown_visibility_preserves_shape_and_claim` |
| H27 | `test_H27_exact_compatibility_message_dedups_distinct_message_is_independent` |
| H28 | `test_H28_delivery_receipt_does_not_resolve_compatibility`<br>`test_H28_delivery_rejection_cannot_clear_compatibility_or_claim_its_resolution` |
| H29 | `test_H29_prior_null_root_claim_survives_refinement_without_repeat` |
| H30 | `test_H30_refinement_after_admitted_claim_keeps_original_null_attribution` |
| H31 | `test_H31_refined_binding_authorizes_fresh_claim_old_null_does_not` |
| H32 | `test_H32_missing_signal_allows_presentation_but_not_result_admission`<br>`test_H32_nonworking_signal_allows_presentation_but_not_result_admission`<br>`test_H32_unreadable_signal_allows_presentation_but_not_result_admission` |
| H33 | `test_H33_saved_stage_address_review_eligible`<br>`test_H33_saved_stage_awaiting_spec_review_eligible`<br>`test_H33_saved_stage_blocked_eligible`<br>`test_H33_saved_stage_canceled_ineligible`<br>`test_H33_saved_stage_done_ineligible`<br>`test_H33_saved_stage_failed_ineligible`<br>`test_H33_saved_stage_implement_eligible`<br>`test_H33_saved_stage_plan_eligible`<br>`test_H33_saved_stage_pr_open_ineligible`<br>`test_H33_saved_stage_queued_eligible`<br>`test_H33_saved_stage_review_eligible`<br>`test_H33_saved_stage_spec_eligible`<br>`test_H33_saved_stage_stalled_on_budget_eligible` |
| H34 | `test_H34_invalid_runtime_evidence_grants_no_presentation`<br>`test_H34_invalid_task_evidence_grants_no_presentation`<br>`test_H34_missing_runtime_evidence_grants_no_presentation`<br>`test_H34_missing_task_evidence_grants_no_presentation`<br>`test_H34_unreadable_runtime_evidence_grants_no_presentation`<br>`test_H34_unreadable_task_evidence_grants_no_presentation` |
| H35 | `test_H35_exact_container_claim_is_403_and_cannot_consume_notice`<br>`test_H35_raw_no_header_claim_is_403_without_mutation` |
| H36 | `test_H36_already_claimed_phase_returns_false_with_current_revision`<br>`test_H36_concurrent_host_claims_have_one_newly_durable_true`<br>`test_H36_foreign_binding_has_no_claim_and_current_binding_can_claim`<br>`test_H36_receipt_resolution_phase_requires_own_confirmed_receipt`<br>`test_H36_resolved_condition_cannot_claim_pending_phase`<br>`test_H36_retired_launch_denies_fresh_revision_claim_while_owned_control_can_claim`<br>`test_H36_stale_revision_has_no_claim_and_fresh_revision_can_claim` |
| H37 | `test_H37_dry_run_has_no_claim_history_notice_or_queue_change` |
| H38 | `test_H38_compatibility_audit_retains_kind_message_without_receipt_resolution`<br>`test_H38_pending_audit_has_typed_identity_diagnostic_and_original_binding`<br>`test_H38_resolved_audit_is_bound_to_own_confirmed_receipt` |
| H39 | `test_H39_compatibility_rendering_does_not_invent_delivery_uncertainty`<br>`test_H39_delivery_context_and_rendering_show_uncertainty_without_false_authority` |
| H40 | `test_H40_presentation_preserves_real_task_queue_receipts_workers_and_clock` |
| H41 | `test_H41_public_direct_entrypoint_has_no_unrelated_pass_dependency`<br>`test_H41_real_pass_reuses_public_entrypoint_before_normal_task_drive` |
| H42 | `test_H42_actual_pass_preserves_existing_180_minute_cap_note`<br>`test_H42_answers_request_keeps_its_existing_artifact_and_closes_background_launch`<br>`test_H42_background_clock_exact_cap_holds_and_strictly_greater_retires`<br>`test_H42_current_background_wait_holds_capacity_and_queued_input`<br>`test_H42_delivered_failure_does_not_reset_exhausted_gate_loop`<br>`test_H42_done_ticket_closes_old_launch_and_cannot_be_revived_by_late_outcome`<br>`test_H42_explicit_ci_wait_retains_run_and_closure_authority`<br>`test_H42_explicit_stage_help_still_parks_with_its_note_over_running_work`<br>`test_H42_foreground_main_remains_exempt_from_old_background_cap`<br>`test_H42_merged_pr_retains_existing_terminal_task_transition`<br>`test_H42_spec_review_request_preserves_artifact_and_operator_authority` |

H21/H36 retired and replaced-event checks also reinforce each other. H18/H19 provide
physical-launch/ticket callback isolation; H22 checks each known post-claim association
change independently. H02 uses opposite public condition-observation orders rather
than illegally rewriting runtime alert lists. H42 has separate strict cap, foreground,
capacity/queue, cap-note, help, CI, spec review, answers, loop-limit, done-ticket and
merged-PR cases.

Native/controller RPC stand-ins and recording session objects count only as HOST evidence.
The 11 already-green H42 cases are targeted regressions and receive no mutation
credit, per Root's explicit ruling. No native pass or full implementation gate is
claimed. Author implementation and genuine native integration remain outstanding.
