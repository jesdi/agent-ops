# 0015. Admission stops at the task token

Date: 2026-11-13

## Context

Woken resumes fail one task on an action exception; feedback spawning deliberately lets an exception leave the pass. A shared spawn action would hide that distinction.

## Decision

The shared admission path chooses a launch, parks stranded tasks, checks capacity and slots, and obtains the task token. It then yields to the caller. `_resume_woken` owns resume, execution-choice consumption and per-task crash handling; `_spawn_feedback` owns its ordered fresh-round action and propagating exceptions.

## Consequences

Admission exceptions retain their existing propagation behavior. Queue-specific actions and their exception boundaries stay outside the generator. The launch chooser supplies `(launch, why)`; feedback discards the unused bypass result.
