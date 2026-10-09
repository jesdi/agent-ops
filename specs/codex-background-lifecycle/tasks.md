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
- [ ] T2 — Launch-scoped atomic snapshots and native Claude prompt hooks preserve binding/clock.
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
- [ ] T3 — Controlled Codex backend and real terminal share one bound conversation. Supervisor
  launches app-server/controller/remote TUI inside task container, with read-only relay/dependency
  mounts and no runtime snapshots mounted. Explicit resume/fresh fallback is retained. Service
  death ends session; incompatible required interfaces report unknown/alert. Isolated fake-provider
  integration proves same terminal sees continuation.
  Seam: Sessions.spawn_stage/resume; containers.session_cmd; supervisor executable.
  Touches: sessions._launch; containers.session_cmd; runtime supervisor/transport/gateway.
  Blocked by: T2.
- [ ] T4 — Gateway orders operator input against conditional retirement. Accepted input prevents
  old-stop park; revision change/unknown state holds; retired rejects later main input. All terminal
  turn RPCs and controller input traverse the same launch fence. Crash and stage gates remain.
  Seam: RuntimeControl.accept_input/retire; gateway RPC; main.run_pass.
  Touches: runtime_control; gateway; main._drive_task.
  Blocked by: T3.
- [ ] T5 — Owned commands and descendants keep sessions alive with strict cap boundaries.
  Commands use owner thread/initial item and qualify only after surviving matching normal
  main Stop in authoritative inventory; previously qualified workers remain deliverable during
  later active turns. Acceptance includes foreground-output-no-redelivery for commands ending
  before first qualifying Stop. Descendants require validated ancestry at every depth.
  Unknown/missing inventory holds. First stopped report starts clock; only newly reported work
  resets it. Same/subset work, foreground input, partial completion, delivery/reconnect preserve it.
  Exact cap holds, greater-than parks; foreground main is never capped. Nonworking stage gates win.
  Seam: RuntimeControl.event/view/retire; controller inventory; main.run_pass.
  Touches: runtime_control; controller inventory; main runtime decision.
  Blocked by: T4.
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
