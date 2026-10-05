# Provider priority: choose which subscription the box spends first

## Why

The box routes each stage to the first entry of its list that the usage gate admits, so the
order written in `targets.yaml` is the only priority there is. Today that order is "Claude
first, GPT-6 as fallback" on every track: OpenAI runs only when Anthropic is denied. A weekly
window that is not used before it resets is lost, and the operator has no way to tell the box
"spend OpenAI now, its week ends tomorrow" short of editing `targets.yaml` over SSH and
remembering to edit it back. The box also never notices by itself that one subscription is far
behind its week while the other is on pace.

## For whom

The operator of the box, who pays for both subscriptions and watches their weekly windows in the
console's usage panel.

## Goal

The box orders every stage's entries by a provider priority mode the operator picks in the
console: **auto** (the default), which puts first the entry whose weekly quota most needs
spending before its reset, or a fixed **provider first** mode. The usage gate keeps deciding
what may run.

## Non-goals

- Not exclusive routing. A priority only reorders a list; it never makes a task wait that would
  run today, and never launches a model outside the stage's list.
- No change to the usage gate: allowance, headroom, the pace margin and the session threshold
  are untouched. A priority provider that is over its pace is still denied.
- Review independence is kept. Review still moves the provider that ran implement to the back,
  whatever the mode.
- No "as written in `targets.yaml`" mode and no per-track opt-out. A track that must stay on one
  provider lists only that provider's entries.
- No per-target mode. The mode is one value for the whole box.
- No automatic expiry of a fixed mode. It stays until the operator changes it.
- No projection from recent burn rate. The box keeps no usage history, and auto ranks on the
  current reading only.
- No change to a stage that already has a pick, and no change to the one-shot "run with this
  model" override.
- No `targets.yaml` key for the mode. The console is the only way to set it. (The session week
  share is a `targets.yaml` value, but it describes the plan, not the mode.)
- No measuring of the session week share. The operator writes the estimate; the box does not
  derive it from usage history.
- No database for box configuration. The mode is stored as a file in the state dir; moving
  config into a database is a separate decision.
