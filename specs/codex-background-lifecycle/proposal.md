# Bound task turns and Codex background results

## Why

portfolio_eval #370 and #384 were parked as "session stopped mid-stage waiting for input"
while their Codex main turns were still working. A waiting marker can come from an earlier
main turn or another Codex thread, and the dispatcher treats that marker as a current turn
end. An isolated test on the box's codex-cli 0.156.1 reproduced a foreign ping from an internal
title thread; it does not identify the producer of either historical ping. Native Codex Stop
hooks identify the session and turn, but do not list background work. The installed app-server
can report running background terminals, completion outcomes, and conversation history, and
can accept a continuation or input within an active turn. The operator needs the task to keep
working, consume its background results, and request help only under the agreed task rules.

Discovery and user decisions are preserved in Engram observations #647, #650, #651, #653,
#654, #656–#661, #664, #666, and #667. Probe scripts and captured evidence are in
`/tmp/agent-ops-handoff-wrongful-park/`. The app-server choice uses experimental interfaces;
the user accepted it with a tested-version compatibility check and acceptance tests. See the
[Codex hooks reference](https://learn.chatgpt.com/docs/hooks) and
[app-server reference](https://learn.chatgpt.com/docs/app-server).

## For whom

The box operator, who expects a task to remain In progress while its main agent or background
workers can continue the current stage, and to receive a clear alert when result delivery
needs attention.

## Goal

Prevent false stopped-mid-stage parks by tying turn-end decisions to the task's current bound
main conversation and turn. Let Codex consume results from its background commands and spawned
agents without operator action, while preserving the existing background-wait cap and task
limits.

This slice builds on [session isolation, #154](https://github.com/jesdi/agent-ops/issues/154).
Its implementation is already present in this stacked draft's `fix/session-isolation`
base (`558b297`). Reuse its explicit resume IDs and continue-or-restart behavior; replace
its Codex fallback that reports unidentified notifications as turn ends. A late or unidentified
notification must not become evidence that the current main turn ended.

## Non-goals

- Auditing what #384 did with another task's context, or repairing polluted conversations.
- A box-wide cap on Codex sessions, or the concurrent authentication refresh check in #155.
- Discovering arbitrary detached operating-system processes. Background work in this slice is
  Codex-managed commands and spawned agent descendants owned by the bound task conversation.
- Changing Claude's existing background-result continuation mechanism, the 10-minute
  dispatcher pass interval, admission/capacity rules, gate/review retry limits, or stage gates.
- Automatic recovery of a dead session service. Existing Failed and operator Resume behavior
  applies; a control-client disconnect with a live service is a different case.
- A guarantee that Codex deduplicates a repeated delivery ID. The tested version accepts the
  same client message ID in multiple turns; the controller must reconcile uncertain delivery.
- Deployment, live task resumes, or changes to the separate #154 worktree.
