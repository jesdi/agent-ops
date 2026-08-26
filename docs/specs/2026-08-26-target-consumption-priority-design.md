# Target consumption priority — priority rail (design)

Date: 2026-08-26
Status: approach B chosen from the interactive prototype
(`docs/prototypes/target-priority-prototype.html`); spec pending review.
Depends on: `2026-08-11-multi-target-onboarding-design.md` (needs ≥2
targets to matter).

## Goal

Let the operator order projects so the box consumes tasks from the
highest-priority project first and falls back to the next one only when
the higher one has no actionable (Ready, unblocked, unquarantined) work.
Surface that order — and which project is currently feeding the box — on
the console, editable in place.

## What exists today (verified)

Strict priority is already the dispatcher's behaviour, implicitly:
`run_pass` iterates `cfg.targets` in **targets.yaml list order**
(`dispatcher/main.py:1307`) and `_claim_new` fills all free capacity
from each target's candidates before the loop reaches the next target.
`next_claim` (`web/read_model.py:507`) walks the cached queues in the
same order. So the semantics need no new scheduler — the work is making
the order explicit, operator-editable without a shell session, and
visible.

## Design

### Priority storage

- targets.yaml list order remains the **default** priority.
- The operator override lives at `$STATE_DIR/target-priority.json`: a
  JSON ordered list of target names, written only by the web API.
- A pure function `apply_priority(targets, override) -> list[Target]`
  (new, in `dispatcher/config.py`) resolves the effective order:
  - names in the override keep that order;
  - configured targets absent from the override are appended in
    targets.yaml order (a new target is consumable immediately, at the
    tail — fail-safe);
  - override names that match no configured target are ignored (a
    removed target cannot wedge the pass).
- `run_pass` and the web read model both order targets through this one
  function, so the dispatcher and the console can never disagree.

### API

- The board payload gains `targets: [{name, actionable, feeding}]` in
  effective priority order. `feeding` is true for the first target with
  `actionable > 0` (at most one).
- `PUT /api/targets/priority` with `{"order": [names...]}` — validates
  that every name is a configured target (400 otherwise), writes the
  file atomically, appends a `priority-changed` event to the event log.
  Full-list replace, no partial patch: the rail always submits the whole
  order, which makes concurrent edits last-writer-wins on the entire
  list instead of interleaving.

### Frontend (per prototype variant B)

- New `PriorityRail` component on `BoardPage`: ordered list of projects
  with drag handles and ▲▼ buttons, per-row actionable count, a
  `feeding` badge on the live source, and `skipped ↓` on dry projects.
  Reorder issues the PUT and refetches the board.
- The target filter tabs (dogfood issue from the onboarding spec) stay a
  **pure view filter** — filtering never changes consumption, and the
  rail never filters the view.
- Queued ghosts render grouped in priority order (they already follow
  queue order; the read model's queue order becomes the effective
  priority order).

### Decision to confirm at review

**Boost stays within-target.** A boosted issue rises within its
project's queue but never jumps the priority fence — strict semantics,
matching the prototype. The escape hatch for "do this agent_ops issue
before portfolio_eval's queue" is dragging agent_ops to the top of the
rail (or the existing per-issue `Next`, which also stays within-target).
If cross-target jumping turns out to be wanted, it is a separate,
explicit feature — not a Boost side effect.

## Error handling

- Missing/corrupt `target-priority.json` → targets.yaml order (log once
  per pass, no failure): the box must never stop claiming because a
  state file rotted.
- The rail's PUT failing surfaces inline on the rail (same pattern as
  the queue action error chip); the board keeps rendering the last
  known order.

## Testing

- pytest: `apply_priority` (override respected; unknown names dropped;
  missing names appended; corrupt file → default), read-model target
  rows (`actionable` counts, single `feeding`, ordering), PUT validation
  + atomic write + event log entry.
- vitest: rail rendering (order, counts, feeding/skipped badges),
  reorder → PUT payload, error chip on failure.

## Out of scope

- Per-project pause, capacity shares, or weighted (non-strict)
  scheduling — the rail's layout leaves room for them later.
- Cross-target Boost/Next (see decision above).
- Any change to ranking within a target.
