# 0016. Wakes and feedback share one slot rule

Date: 2026-11-13

## Context

A parked task can record a slot that another task now holds. Reusing that number blindly can collide on ports; always allocating afresh also changes the number a resumed conversation knows.

## Decision

Both queues retain a recorded slot only when it is not NO_SLOT and no other task satisfying holds_slot records the same number. Otherwise allocate a free slot under max_slots(capacity). Mark no-free-slot refusal and continue considering later candidates. The operator selected this rule at the plan review on 2026-11-13.

## Consequences

Wakes no longer reuse another holder’s slot. Feedback keeps a free recorded number instead of always taking the lowest free one. Slot identity is checked across repositories, excluding only the same repository/issue task.
