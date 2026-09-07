---
name: deep-quality-review
description: >-
  Extremely strict maintainability review of a branch's changes: duplicated
  concepts, complexity that got rearranged instead of deleted, spaghetti
  branching, boundary leaks, weak type contracts, file sprawl, and "temporary"
  fixes that will outlive their reason. Use when the user asks for a "deep code
  quality review", "thermonuclear review", "harsh maintainability review",
  "code judo", "is this making the codebase messier", or before merging a
  change that touches shared code. Not a correctness, security, or test-coverage
  review.
---

# Deep Quality Review

## Overview

One principle: **a change should leave the codebase with fewer concepts, not
more.** Behavior stays the same; structure gets simpler. Judge the diff by the
shape the code will have in a year, when the deadline, migration, or incident
that motivated it is forgotten.

Be ambitious. Do not stop at "this could be a bit cleaner" — look for the
"code judo" move: a reframing that uses the existing architecture so whole
branches, helpers, flags, or layers disappear. If complexity can be deleted
rather than rearranged, push for deletion.

## Scope

**In:** structure, abstractions, duplication, layering, type contracts, file
size, orchestration.

**Out:** correctness bugs, security, tests. Raise one only when structure
causes it (an invariant enforced by an optional parameter is a structure
finding). Everything else belongs in a different review.

## The smells

Run every meaningful change through this table, top to bottom. Each row is one
concept; the *Ask* is the question that exposes it, the *Remedy* is what to
push for.

| # | Smell | Ask | Remedy |
|---|-------|-----|--------|
| 1 | **Duplicated concept** — one idea expressed twice: near-duplicate helper, parallel model, copy-pasted branch, a flag *and* an optional both meaning "off", the same predicate reimplemented instead of reused | What single concept are these copies of? Where is its canonical home? | Name the concept once; make the variants its data or parameters; delete the copies. Never accept a second copy because the first one exists. |
| 2 | **Complexity rearranged, not deleted** — the diff moves code around but the reader still holds the same number of branches, modes, and helpers | Which reframing of the model or ownership makes these branches disappear? | Change the model so the special case becomes the default flow. Prefer the version that feels inevitable in hindsight. |
| 3 | **Special case in a shared flow** — new `if`, one-off boolean, nullable mode, or feature check bolted into an unrelated path | Whose decision is this, and why is it made here? | Move it behind its own abstraction (policy, resolver, dispatcher, module); keep the shared path linear. |
| 4 | **Wrong layer / boundary leak** — feature logic in a shared module, implementation details through an API, callers re-deriving what the owner already knows | Which module owns this concept? | Move the logic to the owner; callers receive results, not raw inputs to recompute. |
| 5 | **Indirection without clarity** — thin wrapper, pass-through helper, "generic" mechanism hiding a simple data shape | What does the reader learn from this layer? | Delete it and keep the direct call. Boring and direct beats clever. |
| 6 | **Weak contract** — `any`, `unknown`, casts, optional params, silent fallbacks that paper over an unclear invariant | What invariant is this hiding? | Make the boundary explicit in the type so the control flow simplifies. |
| 7 | **File sprawl** — the diff pushes a file past a healthy size boundary (~1k lines) | Should this be decomposed *before* adding to it? | Split by concept first; waive only with a compelling structural reason. |
| 8 | **Needless sequencing / non-atomic update** — independent work serialized, related updates that can leave state half-applied | Is this actually dependent? Can it be observed half-done? | Parallelize independent work; group related updates. Not micro-optimization — brittleness. |
| 9 | **Short-horizon change** — "temporary", TODO-later, quick patch, deferred cleanup, a shape chosen because it is the smallest diff | What does this look like when its reason is gone? What does removing it cost? | Do the durable fix now, or structure the stopgap so removal is a pure deletion (one branch, one file), never a refactor of live callers. |

Short-horizon change is the long-term rule and it applies to every other smell: prefer
consolidating now over a third copy later; prefer the simplification that
still makes sense after the migration; never trade a durable simplification
for a smaller diff.

## Process

1. Read the full diff, then the files it touches and the canonical helpers it
   should have reused. Structural problems are only visible in context.
2. For each meaningful change, ask the table's questions in order.
   Duplicated concept and Complexity rearranged are where the big wins hide;
   spend most of the effort there.
3. For every finding, write the *restructuring*, not the patch. "Extract a
   shared helper" is a patch; "one `Message` model with a renderer per channel,
   so channels become data" is a restructuring.

## Output Expectations

Write the review in this shape:

1. **Findings**, in the table's order (Duplicated concept first,
   Short-horizon change last). Lead each finding with the smell's **name**
   in bold (e.g. **Duplicated concept**, **Weak contract**), never a row
   number — the table's numbers only sequence the review; the names carry
   the meaning to a reader who has not seen the table. A finding that spans
   two smells names both (**Weak contract + Short-horizon change**). Then:
   where, and the concrete restructuring.
   Keep to the few high-conviction findings — structural findings displace
   cosmetic ones, and a cosmetic finding never appears while a structural
   one exists.
2. **Verdict**: `APPROVE` or `REQUEST CHANGES`, one line of justification.

## Approval Bar

Working code is not the bar. `REQUEST CHANGES` when any of Duplicated
concept, Complexity rearranged, Special case in a shared flow, Wrong layer,
Indirection without clarity, Weak contract, or Short-horizon change is present
and unjustified, or when a visible code-judo move was skipped. File sprawl and
Needless sequencing block when the cleaner structure is obvious.

## Author pushback

| Pushback | Answer |
|----------|--------|
| "It works, tests pass" | The bar is structure. Working code that makes the codebase messier is a regression. |
| "We'll clean it up after the migration / next quarter" | Cleanup later is a refactor of live callers; the durable version is cheaper now. |
| "It's just one flag / one `if`" | Every flag is a mode every future reader must hold in their head. |
| "The existing code already does it this way" | Existing mess licenses fixing the concept, not adding a copy of it. |
| "A restructure is out of scope" | If the restructure deletes the complexity this change adds, it is the scope. |

## Review Tone

Be direct, serious, and demanding about quality.
