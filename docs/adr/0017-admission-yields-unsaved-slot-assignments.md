# 0017. Admission yields unsaved slot assignments

Date: 2026-11-13

## Context

Admission may select a slot before a task token is available. Persisting an admission decision separately from the action would change skipped-task state.

## Decision

The generator yields the task carrying its selected slot without saving that assignment. The caller’s existing spawn or resume action owns saving it. Selecting a slot alone must not write task state.

## Consequences

A skipped admission does not persist a speculative assignment. Existing parking helpers still own their state transitions; admission adds no new file or marker.
