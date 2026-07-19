---
name: file-bug-issue
description: >-
  Open (or update) a GitHub issue whenever a bug or misbehaviour is detected while
  working in a repo. Use this PROACTIVELY — the moment you notice something broken
  (a failing test that reveals a real defect, a crash, a stack trace, wrong output,
  a UI glitch, a regression, an API returning the wrong thing, flaky behaviour, a
  "that shouldn't happen") — not only when the user says "file a bug". It first
  searches the repo's OPEN issues (filtered by the labels that fit the bug) and
  judges whether the same defect is already tracked. If a real duplicate exists, it
  adds your new reproduction/context as a comment; otherwise it creates a fresh,
  well-structured issue with reproducible steps and the context a fixer will need.
  Trigger phrases include "file an issue", "report this bug", "log this", "open a
  ticket", "track this", "this is broken", "that's a bug" — and also fire with no
  prompt at all when you stumble onto a defect mid-task.
---

# File a Bug Issue on GitHub

When you detect a bug or misbehaviour, your job is to make sure it ends up tracked
on GitHub **without creating duplicates**. The flow is always: understand the bug →
find the right repo → search existing open issues → judge for duplicates → comment
on the existing one **or** create a new one.

The point of this skill is leverage: a future fixer (human or agent) should be able
to open the issue and reproduce the problem without re-deriving everything you
already know right now. Capture that context while it's fresh.

## Preconditions

- `gh` must be authenticated for the repo. Verify quickly if unsure: `gh auth status`.
- Run from inside the git repo so `gh` auto-detects it, or pass `--repo owner/name`.
  Confirm the target: `gh repo view --json nameWithOwner -q .nameWithOwner`.

If `gh` isn't available or authenticated, say so and stop — don't silently skip the
tracking step.

## Step 1 — Pin down the bug

Before touching `gh`, get crisp on what actually went wrong. You usually already
have this in your working context; pull it together:

- **What you observed** — the actual wrong behaviour, in one sentence.
- **Where** — `file_path:line`, the route, the component, the endpoint, the command.
- **Reproduction** — the minimal sequence that triggers it. If you triggered it via
  a test, a request, or a UI action, write the exact steps/commands.
- **Expected vs. actual** — what should have happened instead.
- **Evidence** — the stack trace, error message, failing assertion, log line, or
  screenshot description. Trim noise but keep the diagnostic core.
- **Suspected cause / scope** — only if you genuinely have a lead. Don't guess
  loudly; mark speculation as speculation.

If you can't actually describe a reproduction or concrete observation, you probably
don't have a bug worth filing yet — investigate first rather than logging a vague
"something seems off".

## Step 2 — Choose the labels that fit

Labels are how you narrow the duplicate search, so pick them before searching. List
what the repo offers: `gh label list`.

Always include the repo's bug label if it has one (commonly `bug`). Then add any
labels that scope the area — language (`python`, `javascript`/`frontend`),
subsystem, or component — when the repo has matching ones. Only use labels that
already exist; don't invent new ones here.

## Step 3 — Search open issues, filtered by those labels

The goal is a small, high-signal candidate set, not a keyword dragnet. Filter open
issues by the scoping labels, then look:

```bash
# Candidates within the same area (repeat --label to AND them, or widen if empty)
gh issue list --state open --label bug --limit 50 \
  --json number,title,labels,updatedAt

# Also do a focused text search on the distinctive terms of THIS bug
# (error class, function name, symptom) to catch issues labelled differently:
gh issue list --state open --search "TypeError serialize holdings" \
  --json number,title --limit 20
```

If the label-filtered list is empty or tiny, widen: drop the most specific label, or
fall back to a text search over all open issues. You want to be confident you didn't
miss an existing report.

## Step 4 — Judge for a real duplicate

Don't dedup on title similarity alone — read the candidates. Open the few that look
plausible:

```bash
gh issue view <number> --json title,body,labels,comments
```

A candidate is the **same** bug only if it's the same defect: same symptom AND same
root area/trigger. Different symptoms that might share a cause are *not* automatic
duplicates — and two issues with similar words but different reproductions are
distinct. When genuinely unsure, prefer treating it as **new** but link to the
possibly-related issue in your write-up (cheaper to cross-link than to bury a real
bug as a false duplicate).

## Step 5a — If it's a duplicate: comment with the new context

Add what the existing issue is missing — your fresh reproduction, the new
environment, an additional stack trace, "still happening as of <commit/date>", or a
narrower repro. Don't restate what's already there.

```bash
gh issue comment <number> --body "$(cat <<'EOF'
Reproduced again on `<branch>` @ `<short-sha>` (<date>).

**Steps**
1. …

**New context**
- …

**Evidence**
```
<trimmed trace>
```
EOF
)"
```

Report back: the issue number/URL and that you commented (not created).

## Step 5b — If it's new: create the issue

Create it directly (no confirmation needed) with the bug + scope labels and a
structured body. Use this template — keep headings, drop a section only if it
truly doesn't apply:

```markdown
## Summary
<one-sentence description of the wrong behaviour>

## Where
<file:line / route / component / command>

## Steps to reproduce
1. …
2. …
3. …

## Expected
<what should happen>

## Actual
<what happens instead>

## Evidence
` ` `
<stack trace / error / failing assertion / log — trimmed to the diagnostic core>
` ` `

## Environment
<branch @ short-sha, OS, runtime/version, container, browser — whatever's relevant>

## Notes
<suspected cause if you have a real lead; links to related issues #N; mark guesses as guesses>
```

Create it:

```bash
gh issue create \
  --title "<concise, specific: symptom + where, not just 'bug'>" \
  --label bug \
  --body "$(cat <<'EOF'
## Summary
…
EOF
)"
```

Good titles name the symptom and locus: `Holdings serialize crashes with TypeError
when weight is null` beats `Bug in portfolio`. After creation, report the issue
URL back to the user.

## After filing

Tell the user concisely what you did: created vs. commented, the issue number/URL,
and the labels applied. If you were mid-task when you spotted the bug, this is a
side-quest — log it, mention it, and return to the original task unless the user
redirects.

## Guardrails

- **One issue per distinct defect.** Don't bundle unrelated bugs; don't split one
  bug across several issues.
- **Don't fabricate reproductions.** If you couldn't actually reproduce it, say so
  in the issue and label the steps as "observed once / not yet reproduced".
- **Respect the existing tracker.** Match the repo's label and title conventions
  rather than imposing your own.
- **Creating/commenting is outward-facing.** It's authorized to do so without
  asking here, but never delete or close others' issues, and never edit an existing
  issue's body — only add comments.
