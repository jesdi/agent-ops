---
name: backlog
description: >-
  Manage a GitHub-Issues-backed backlog and a Projects-v2 work graph via five
  verbs. Use when the user wants to CAPTURE an idea/research thread ("add to
  backlog", "capture this", "note this idea", "log a task"), TRIAGE the inbox
  ("triage the backlog", "score the inbox"), see what to work on NEXT ("what's
  next", "what should I work on", "ranked backlog", "next actionable task"),
  LAND a finished item ("mark done", "close out #N"), or SETUP the board the
  first time. Ranks open issues by impact÷effort, respecting `Blocked by:`
  edges, so only actionable work surfaces.
---

# backlog — capture + ranked work graph on GitHub

Two needs, one substrate: a frictionless **capture** inbox (loose issues labelled
`inbox`) and a typed, dependency-aware **work graph** (one user-owned Project v2
with Impact / Effort / Area / Status). The verbs below operate on both.

**Dispatch on the user's intent:**

| Intent | Verb |
|--------|------|
| "add this idea", "capture", "note this" | `capture` |
| "triage the inbox", "score these" | `triage` |
| "what's next", "ranked backlog" | `next` |
| "I'm starting #N", beginning work on an issue | `start` |
| "mark #N done", "close it out" | `land` |
| first-time board creation | `setup` |

All exact commands live in `references/graphql.md`. Board IDs come from the
**consuming project's** `.backlog/project-meta.json` (created by `setup`, found
by walking up from the current directory). That file is a **static schema
pointer** — project/field/option IDs only, *not* issue state (Impact/Effort/
Status live on the GitHub board and `rank.py` fetches them live). Because the IDs
are non-secret constants that change only on re-provision, the file is
**committed in the consuming repo** and shared across every branch/worktree;
`project-meta.example.json` is the shape reference. Reads use the existing `gh`
login; **writes need the one-time `gh auth refresh -s project`** (Projects v2 is
not in the default `repo` scope).

`<owner>`, `<repo>`, `<projectNumber>` below all come from that
`.backlog/project-meta.json`.

## `setup` — create the board once (idempotent)

Run when `.backlog/project-meta.json` is absent or the board is being
(re)provisioned. Reconciles rather than duplicates — re-running after adding a
field is safe. Writes into the **project's** `.backlog/` directory, not the skill
dir.

1. Ensure the `project` scope: `gh auth status`; if Projects calls 403, run
   `gh auth refresh -s project`.
2. Follow `references/graphql.md` → **Setup**: create the Project, link the repo,
   add the six fields, create the `inbox` label. On a brand-new board the `Area`
   single-select is created with the generic default options
   `feature,bug,infra,docs,research`. If any step reports "already exists", treat
   it as done and continue. Projects that want different Areas edit the option set
   on the board (GitHub UI) and re-run `setup` to reconcile the IDs.
3. Discover IDs: `gh project view … --format json` (project id) and
   `gh project field-list … --format json` (field + option ids).
4. Build `.backlog/project-meta.json` from that discovery using
   `meta.build_project_meta`. Run from the skill directory (so `import meta`
   resolves), writing to the project's `.backlog/` (create it if missing):

   ```bash
   mkdir -p "$PROJECT_ROOT/.backlog"
   gh project view <projectNumber> --owner <owner> --format json > /tmp/proj.json
   gh project field-list <projectNumber> --owner <owner> --format json > /tmp/fields.json
   python3 - "$PROJECT_ROOT/.backlog/project-meta.json" <<'PY'
   import json, sys, meta
   proj = json.load(open("/tmp/proj.json"))
   fields = json.load(open("/tmp/fields.json"))
   out = meta.build_project_meta(
       "<owner>", "<repo>",
       proj["number"], proj["id"], fields)
   json.dump(out, open(sys.argv[1], "w"), indent=2)
   print("wrote", sys.argv[1])
   PY
   ```

5. Confirm every field + option id in `.backlog/project-meta.json` is non-empty,
   then commit the real file (non-secret schema pointer). Report the Project URL.

6. **Built-in workflows (UI-only — no API exists; ask the user to click):**
   at `https://github.com/users/<owner>/projects/<projectNumber>/workflows`
   enable:
   - *Auto-add to project*: repo `<repo>`, filter `is:issue is:open`
   - *Item added to project*: issues → `Status: Inbox`
   - *Item closed*: issues → `Status: Done`
   - *Item reopened*: issues → `Status: Ready`
   These make the board self-maintaining; `land` is only an escape hatch.

## `land <issue#>` — verify closed + Done (escape hatch)

Closing and Done are **automatic**: a PR merged with `Closes #N` closes the
issue, and the board's "Item closed" built-in workflow sets `Status: Done`.
`land` only verifies and repairs:

1. `gh issue view N --repo <repo> --json state` — if not `CLOSED`, close it:
   `gh issue close N --repo <repo>`.
2. Check board status (`gh project item-list … --jq` per graphql.md → Land);
   if not `Done` after ~1 min (workflows are async), set it via `item-edit`.
3. Report: closed + Done, noting whether automation handled it or you repaired.

## `start <N>` — claim an issue (check-and-claim)

Run when beginning work on an issue — worktree setup time is the natural
moment. The claim signal is the board Status column and nothing else: humans
claim by dragging the card to "In progress"; agents run this verb.

1. Read the item id + current status live (graphql.md → Start).
2. If status is already `In progress` → **abort**: report the issue is
   already claimed and offer `next` for an alternative. Not an atomic lock —
   good enough for one human + a few agents.
3. Otherwise set `Status: In progress` via `item-edit`.
4. `start --release <N>`: same lookup, set `Status: Ready` (abandon a claim).

## `capture <idea>` — file an inbox issue (frictionless)

Reuse the **dedup discipline** from the `file-bug-issue` skill, but take the
*idea* path — no reproduction scaffolding.

1. Shape a concise, specific title from the idea (e.g. "Cache batch
   responses per ticker", not "caching").
2. Dedup: `gh issue list --repo <repo> --state open --search "<distinctive terms>" --json number,title,url`.
   If a genuine duplicate exists, add the new angle as a comment instead of a new
   issue and report which. Different-but-related → create new and cross-link `#N`.
3. Otherwise create it (see `references/graphql.md` → Capture): `--label inbox`,
   body = the idea as-is (one loose sentence is fine; no required structure).
4. Report the new issue URL. If mid-task, treat as a side-quest and return.

## `triage` — turn inbox issues into scored graph nodes

1. List the inbox: `gh issue list --repo <repo> --state open --label inbox --json number,title,url,body`.
2. For each, present title + body and ask whether to promote (skip = leave in inbox).
3. For a promoted issue, gather `Impact` (1–5), `Effort` (1–5), `Area` (**pick one
   from the options present in `.backlog/project-meta.json`** → `fields.Area.options`),
   and optional blockers.

   - **Boost** (number, optional, default 0): manual priority override. Ranking is
     band-dominant — any issue with higher Boost outranks every issue with lower
     Boost regardless of score; score orders issues within a band. Negative values
     sink an issue below the unboosted pack. Dispatchers set 99 for "work on this
     next". `--json` rows carry `"boost"`.
4. Apply, in order, the `references/graphql.md` → Triage commands:
   add to project → set Impact/Effort (`--number`) → set
   `Score` = round(Impact ÷ Effort, 1) (`--number`) → set Area +
   `Status: Ready` (`--single-select-option-id`) → append `Blocked by: #…`
   to the body if any → `--remove-label inbox`.
5. After each, confirm it is now triaged (fields set, `inbox` gone). Report a
   summary of promoted vs. left-in-inbox.

## `next` — ranked, dependency-aware view (read-only)

Runs the pure engine; makes no writes. Run **from the project root** (so the cwd
walk-up finds `.backlog/project-meta.json`); do **not** `cd` into the skill dir:

```bash
python3 .claude/skills/backlog/rank.py          # human-readable table
python3 .claude/skills/backlog/rank.py --json   # machine-readable rows for
                                                # dispatchers: number, title,
                                                # url, status, labels, blocked,
                                                # score, boost
```

(Use whichever agent-skills path the store symlinked the skill into — e.g.
`.agents/skills/backlog/rank.py`. The key is that the working directory stays at
or below the repo root so `rank.py` can walk up to `.backlog/`.)

`rank.py` pulls Project items + issue bodies via `gh`, parses `Blocked by:`
edges, drops closed/`Done` blockers, and prints:

- **Available** issues ranked boost-band first, then Impact÷Effort within a band
  (unscored sort last within a band, flagged `[needs triage]`).
- **Blocked** issues, unranked, annotated with what they wait on.

Present the table as-is. If the user asks "why isn't #N at the top", check first
whether another issue has a higher Boost band (shown as `↑n`), then its score, then
its blockers from the output. This in-memory ranking is the reason the
skill exists over a bare `gh issue list`.
