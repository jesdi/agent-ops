---
name: to-spec
description: "Turn the current conversation into a design spec and write it where the next stage reads it: a GitHub issue body labelled spec-ready (after a grilling session), or a dated design file on the current branch (an unattended spec stage). No interview, just synthesis of what you've already discussed."
---

<!--
Modified copy of https://github.com/mattpocock/skills/tree/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/to-spec
(mattpocock-skills v1.2.3). Copyright (c) 2026 Matt Pocock, MIT — see LICENSE.
Changes: publishes to an issue body or a design file instead of an issue tracker;
seams are stated rather than confirmed when unattended; the document must pass the
agent-ops mechanical spec check; Testing Decisions must name the seams; the
`ready-for-agent` label became `spec-ready`; model invocation is allowed.
-->

This skill takes the current conversation context and codebase understanding and produces a spec. Do NOT interview the user; just synthesize what you already know.

## Destination

Decide where the spec goes before writing it. There are exactly two destinations:

- **Issue body.** Use this when a human settled the design with you interactively (a grilling session) and the unattended pipeline will pick the work up later. Create a GitHub issue in the current repo whose body is the spec, labelled `spec-ready`. If the conversation started from an existing issue, replace that issue's body and add the label instead of creating a new issue. The spec's H1 becomes the issue title and is omitted from the body.
- **Design file.** Use this when the invoking prompt names a file path, or when you are running unattended on a task branch. Write the spec to that path; with no path given, use `docs/specs/<YYYY-MM-DD>-<slug>-design.md`. Keep the H1 as the first line. Commit it to the current branch with a `docs:` message.

If neither is clear, ask once: "Issue body or design file?". Never publish anywhere else.

## Process

1. Explore the repo to understand the current state of the codebase, if you haven't already. Use the project's domain glossary vocabulary throughout the spec, and respect any ADRs in the area you're touching.

2. Sketch out the seams at which you're going to test the feature. Existing seams should be preferred to new ones. Use the highest seam possible. If new seams are needed, propose them at the highest point you can. The fewer seams across the codebase, the better - the ideal number is one.

   With a human present, check that these seams match their expectations. Unattended, state your choice in the Testing Decisions section instead: a later implement session treats that section as the seams already agreed and cannot stop to ask.

3. Write the spec using the template below, then deliver it to the destination.

## Mechanical requirements

The unattended pipeline rejects a spec that fails a format check, so every spec must have:

- One `# <title>` H1 as the first line (file destination) or as the issue title (issue destination).
- At least two `## ` sections. Use every section of the template; write "None." rather than dropping one.
- A **Testing Decisions** section that names each seam explicitly: the module, function, command or endpoint the tests drive through, and whether it exists today or is new.

## Delivery commands

```bash
# Issue body (new issue). Write the body to a temp file first.
gh issue create --repo <owner/repo> --title "<H1 text>" --label spec-ready --body-file /tmp/spec.md

# Issue body (existing issue)
gh issue edit <number> --repo <owner/repo> --body-file /tmp/spec.md --add-label spec-ready

# If the label does not exist yet (the backlog skill's setup normally provisions it)
gh label create spec-ready --repo <owner/repo> --description "Issue body carries a settled design; the pipeline skips its interview" --color 006B75 --force

# Design file
git add docs/specs/<date>-<slug>-design.md && git commit -m "docs: spec for <slug>"
```

Read `<owner/repo>` from `gh repo view --json nameWithOwner -q .nameWithOwner`.

<spec-template>

# <Feature title>

## Problem Statement

The problem that the user is facing, from the user's perspective.

## Solution

The solution to the problem, from the user's perspective.

## User Stories

A LONG, numbered list of user stories. Each user story should be in the format of:

1. As an <actor>, I want a <feature>, so that <benefit>

<user-story-example>
1. As a mobile bank customer, I want to see balance on my accounts, so that I can make better informed decisions about my spending
</user-story-example>

This list of user stories should be extremely extensive and cover all aspects of the feature.

## Implementation Decisions

A list of implementation decisions that were made. This can include:

- The modules that will be built/modified
- The interfaces of those modules that will be modified
- Technical clarifications from the developer
- Architectural decisions
- Schema changes
- API contracts
- Specific interactions

Do NOT include specific file paths or code snippets. They may end up being outdated very quickly.

Exception: if a prototype produced a snippet that encodes a decision more precisely than prose can (state machine, reducer, schema, type shape), inline it within the relevant decision and note briefly that it came from a prototype. Trim to the decision-rich parts, not a working demo, just the important bits.

## Testing Decisions

A list of testing decisions that were made. Include:

- A description of what makes a good test (only test external behavior, not implementation details)
- **The seams**: every interface the tests drive through, named (module, function, command or endpoint), each marked existing or new. This list is the agreement a later implement session works from.
- Which modules will be tested
- Prior art for the tests (i.e. similar types of tests in the codebase)

## Out of Scope

A description of the things that are out of scope for this spec.

## Further Notes

Any further notes about the feature.

</spec-template>
