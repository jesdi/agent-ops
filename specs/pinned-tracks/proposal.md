# Pinned tracks: the kind of work chooses the model

## Why

The provider priority mode treats the two subscriptions as interchangeable: it reorders every
stage list by which weekly quota most needs spending. For some kinds of work they are not
interchangeable. The operator wants frontend and design work on Fable, because it writes the
best frontend code and has the better taste for UX, and wants heavy backend work, architecture
changes and security work on Astra. Today the mode overrides that: in `auto`, or with "OpenAI
first", a frontend task launches on Astra whenever OpenAI ranks higher, and there is no track
for architecture work at all. A task that mixes both kinds also gets one model for all its
tickets, although every ticket runs in its own session.

## For whom

The operator of the box, who decides which model is good at which kind of work and writes that
in `targets.yaml`.

## Goal

A track can be pinned in `targets.yaml`, in a list whose order is also the precedence among
pinned tracks: its stage lists run in the order written in `targets.yaml`, whatever the
priority mode. `frontend`, a new `architecture` track and `security` are pinned in the example
config, and a ticket can name its own pinned track for the implement stage, so each part of a
mixed task runs on the model chosen for that kind of work.

## Non-goals

- No change to the usage gate. A pinned entry that is over its allowance is still denied.
- No new fallback rule. The written list is the fallback chain; when no entry of it is
  admitted, the task waits. A pinned track never launches a model outside its list.
- No change to picks or to the one-shot "run with this model" override. Both still win.
- No pin per stage. A pinned track is pinned for all four stages.
- No pin control in the console. The pin is a `targets.yaml` value; the console only shows it.
- No pin for `models.triage`. The triage list is still ordered by the priority mode.
- No per-ticket track for spec, plan or review. Those stages use the task's track.
- No ticket may name an unpinned track. A ticket cannot move work to `trivial` or `standard`.
- No "avoid the provider that ran most tickets" rule for review. When implement used more than
  one provider, review avoids none.
- No automatic classification by the dispatcher. Triage, the spec session and the plan session
  still choose the track from the operator's `when:` sentences.
- No change to the box's live `targets.yaml` or to the infra seed config. The operator applies
  those; this change updates `targets.example.yaml` only.
