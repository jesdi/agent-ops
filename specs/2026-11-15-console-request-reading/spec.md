# One request reading for console board helpers — Spec

## Requirements

1. **One context per request.** The board helper family receives one request-scoped value containing the effective configuration, captured UTC time, priority mode and derived ordering, and usage when the caller needs it. Resolve effective configuration once per participating request; read priority mode once when needed, fetch usage at most once, and build the ordering once when needed. Helpers must not independently reload those inputs or obtain another calculation time. The value is not retained across requests. Scenarios: one full reading; next request sees changes.
2. **One configuration authority.** Model choices, project-policy lookup, pinned-track reporting, task and candidate admission, usage presentation, ordering, capacity and the full board's `max_open` use the reading's configuration. The calculation helpers must not capture `boot` or accept a separate configuration, ordering, usage or timestamp beside the reading. Task, repository, row and queue arguments remain domain inputs. This includes `_choices`, `_model_for`, `_task_admission`, `_pinned_track`, `_admission_for_model`, `_candidate_choices`, `_candidate_model`, `_candidate_admissions` and `_usage_view`, or their replacements. Configuration, mode and usage acquisition belong at the reading-construction boundary; no half-built reading is required. Scenarios: effective configuration is authoritative; helper contract.
3. **Preserve the fast snapshot.** `/api/board/snapshot` builds a reading without fetching usage, GitHub rankings or session liveness. Missing usage in this path means selection without admission filtering; it is distinct from an actual empty/unavailable usage result on the full path. Fixed priority modes still reorder entries, auto without usage retains written order, and pins, existing picks and one-shot overrides retain their precedence. Scenarios: local snapshot; unavailable full reading.
4. **Migrate existing shared callers.** Apply the same ownership rule to `/api/board`, `/api/board/snapshot`, `/api/task/{repository}/{issue}`, `/api/usage`, the socket app's `/usage`, and the existing run-model selection path that calls these helpers. Run requests with an explicit model or a queued candidate must not gain unnecessary usage fetching. Preserve explicit-model validation, same-provider restrictions, saved picks including effort, and queued-candidate selection without usage. Scenarios: shared callers; run selection.
5. **Preserve externally visible behavior.** For unchanged source inputs, retain response schemas and existing selection/admission rules: project policies override the global policy, a saved pick or one-shot model wins over ordinary selection, pinned tracks keep their defined order, usage bypass suppresses the admission warning, and unavailable usage follows existing fail-closed admission rules. Preserve removed-repository fallback to the global policy. Scenarios: precedence; unavailable full reading; shared callers.

The current code matches the issue's premise: `_cfg()` applies stored model settings to `boot`, and `apply_stored_models` changes only models and origin metadata. Repository membership is consequently startup-stable today. This refactor does not add targets.yaml reload or atomic reads across settings, priority, tasks and usage. The new request context must be distinguished from the unrelated per-window `dispatcher.usage.Reading` and the usage-adapter terminology in `CONTEXT.md`; its exact Python name and location belong to the plan stage.

Testing decisions: drive behavior through the existing FastAPI TestClient routes with `tests.webfakes.FakeSources`, controlled config/priority loaders and deterministic time/usage. Extend the existing snapshot and once-per-request coverage in `tests/test_web_routes_read.py`, and reuse priority, pinned-track, run and model-settings tests for compatibility. A deliberately different effective-config fixture is a dependency-ownership test, not a claim that pace or capacity can be edited live. Check the helper signature/dependency contract in review; do not lock tests to the name or placement of private helpers. Implementation uses failing tests first for newly enforced behavior and runs the repository gate `make gate` after each ticket. This spec-only stage changes no code and needs no runtime gate.

## Scenarios

### Scenario: One full reading (requirement 1)

- **Given** two active tasks, Ready candidate 73, a controlled clock at `2026-11-15T12:00:00Z`, and loaders that record config, mode and usage acquisition.
- **When** one `/api/board` request renders models and admission warnings.
- **Then** config, priority and usage are each acquired once, ordering is derived once, and every time-dependent decision uses the captured time, even if a source callback changes the underlying settings or clock during rendering.

### Scenario: Next request sees changes (requirement 1)

- **Given** request A resolves a valid policy selecting `anthropic/claude-opus-5` for an unpicked task, and a valid stored policy selecting `openai/gpt-luna` replaces it before request B.
- **When** two sequential board requests complete with both providers admitted.
- **Then** A consistently uses its original policy and B uses the replacement; no application-wide reading cache holds A's inputs.

### Scenario: Effective configuration is authoritative (requirement 2)

- **Given** startup capacity 2 and a test-injected effective configuration with capacity 4, a distinct `max_open`, and pace thresholds that produce a different admission verdict for the same usage.
- **When** the board, snapshot and usage/detail views are requested with controlled inputs.
- **Then** board and snapshot report capacity 4, full-board claim forecasting honors the effective `max_open`, and ordering, gate and task/candidate warnings use effective pace throughout; no calculation falls back to startup pace.

### Scenario: Helper contract (requirement 2)

- **Given** the migrated helper family and all its callers.
- **When** their signatures and dependencies are reviewed.
- **Then** each calculation receives one request context plus only its domain arguments, uses no captured startup configuration and performs no independent configuration, priority, usage or clock acquisition. All calls share the context their request constructed.

### Scenario: Local snapshot (requirement 3)

- **Given** usage, ranking and session-probe fakes raise if called, and an unpicked task has two providers in written order.
- **When** `/api/board/snapshot` is requested first in auto mode and then in a fixed mode prioritizing the second provider.
- **Then** both return 200 without those probes; auto names the first written entry and fixed mode names its preferred entry, with unchanged snapshot fields. A pinned track retains its written order in either mode.

### Scenario: Unavailable full reading (requirements 3 and 5)

- **Given** actual usage is an empty map or contains only unavailable providers, and a nonterminal task and candidate 73 have no bypass.
- **When** `/api/board` is requested.
- **Then** existing fail-closed gate/admission results are preserved; actual unavailable usage is never mistaken for the snapshot's intentionally uncollected usage.

### Scenario: Shared callers (requirements 4 and 5)

- **Given** fixed effective settings and usage for task `alpha/7`.
- **When** board, task detail, authenticated usage and socket usage are requested independently.
- **Then** each request obtains its own single reading, board and detail agree on the model/admission under identical inputs, both usage routes expose the same usage result, and public response fields and access boundaries remain unchanged.

### Scenario: Run selection (requirement 4)

- **Given** an explicit configured model request, a task with saved pick `openai/gpt-luna@high`, an unpicked task, and queued candidate 73.
- **When** the existing run endpoint selects a model for each case.
- **Then** the explicit-model case preserves validation without fetching usage, the saved pick retains its effort, the unpicked task selects as the full board does under identical inputs, and the queued candidate follows its policy and mode without fetching usage. A forbidden cross-provider override is still rejected.

### Scenario: Precedence (requirement 5)

- **Given** tasks with a project-specific policy, a saved pick, a pending one-shot override, a pinned track, and an override with usage bypass, plus a task from a removed repository.
- **When** board and detail calculate their models, pins and admissions under controlled usage.
- **Then** project policy wins over global policy, saved picks and one-shot overrides keep their existing precedence, pinned order is retained, bypass omits the admission warning, and the removed repository uses the global policy exactly as before.

## Out of scope

No frontend or API schema changes; no new runtime policy, targets.yaml hot reload, source-wide transactional snapshot, shared cache, persistence, authentication or settings-write changes. No redesign of unrelated routes, dispatcher admission logic, or `dispatcher.usage.Reading`. This stage writes only proposal and spec; design and tickets belong to plan. There are no open operator decisions.

## Implementation ruling (2026-11-16)

The operator reviewed ticket 03 after its test-writer reported that three task-detail HTTP characterization probes passed before implementation. Those results show the listed public behavior is already satisfied; they are not evidence for inventing a new HTTP behavior change. Keep all ticket 03 acceptance criteria as compatibility invariants while completing the behavior-preserving request-reading helper/interface refactor. Verify the helper-signature and dependency contract through type checking, static inspection and independent code review, while retaining the existing route invariants. The test-writer report records the probes and their removal as already-satisfied behavior.

The operator and independent arbiter then authorized the same classification for tickets 04 and 05 if bounded route evidence confirmed their criteria. The saved `.agent/manual-finalization-187-probe.py` and its output confirm ticket 04's per-request acquisitions, effective pace and captured time, TCP/socket parity and access boundaries, plus ticket 05's explicit-model usage bypass, same-provider refusal, unpicked board parity, and queued-candidate effective project policy/priority without usage. The saved 21-test run is `.agent/manual-finalization-187-targeted-tests.log`; the helper dependency check is `.agent/manual-finalization-187-static.py` and `.agent/manual-finalization-187-static.log`. No behavior-missing failure was found or invented. Only the Seam classification and verification note changed in tickets 04/05; every acceptance criterion remains a required compatibility invariant. The shared helper contract is checked statically and remains subject to independent final review. No product or test source changed during this manual characterization.
