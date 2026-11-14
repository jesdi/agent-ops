# 0015. A transition acts, commits once, then notifies

Date: 2026-11-11

## Context

A task's turn saved its file several times, between launches and
notifications. A failure between two saves left states no code expected, and
`updated_at`, which the plan gate's grace time and the wake order read, was
restamped by writes that changed nothing.

## Decision

- The executor runs the actions that end or start a session, then saves the
  task once and appends one `transition` event, then sends notifications.
- An action that raises saves nothing of the transition and becomes an event
  itself (`launch-failed`, `task-crashed`).
- `updated_at` changes only when the status kind or the stage changes.

## Consequences

- A dispatcher killed before the save repeats the launch on the next pass. It
  never leaves a `running` task with no session.
- A notification can be lost by a crash after the save; it is never sent for
  a transition that did not commit.
- Actions must be safe to repeat.
