---
name: prototype
description: Build a throwaway prototype to answer a design question, delivered as one self-contained HTML file. Use when the user wants to sanity-check whether a state model or logic feels right, or explore what a UI should look like.
---

<!--
Modified copy of https://github.com/mattpocock/skills/tree/3cca18b368ae95cdbdebbff572ccafa662551015/skills/engineering/prototype
(mattpocock-skills v1.2.3). Copyright (c) 2026 Matt Pocock, MIT — see LICENSE.
Changes: the UI branch produces one self-contained HTML file holding every
variation (no routes, no dev server) instead of routes in the running app; the
file is written to the path the invoking prompt names; the verdict is captured as
an issue comment instead of a throwaway branch.
-->

# Prototype

A prototype is **throwaway code that answers a question**. The question decides the shape, but the delivery is always the same: **one self-contained HTML file** that opens by double-click, with no build step and no server. That is what lets a machine with no browser serve it as a file and a person open it on a phone.

## Pick a branch

Identify which question is being answered, using the user's prompt, the surrounding code, or by asking if the user is around:

- **"Does this logic / state model feel right?"** → [LOGIC.md](LOGIC.md). Free-play buttons plus tabbed guided walkthroughs that push the state machine through cases that are hard to reason about on paper, and that a non-developer can drive.
- **"What should this look like?"** → [UI.md](UI.md). Several radically different UI variations in one page, switchable inside the page from a floating bottom bar.

The two branches produce very different pages, so getting this wrong wastes the whole prototype. If the question is genuinely ambiguous and the user isn't reachable, default to whichever branch better matches the surrounding code (a backend module → logic; a page or component → UI) and state the assumption at the top of the prototype.

## Rules that apply to both

1. **One file, clearly a prototype.** Everything inline: HTML, CSS, JS, sample data. No framework, no bundler, no server, no network requests. Write it to the path the invoking prompt names; with no path given, put it next to the module or page it prototypes for, named `<name>.prototype.html`, so a casual reader sees it is a prototype and not production.
2. **Trivial to run.** Opening the file is the whole procedure. No thinking required to start it.
3. **No persistence by default.** State lives in memory. Persistence is the thing the prototype is _checking_, not something it should depend on.
4. **Skip the polish.** No tests, no error handling beyond what makes the prototype _runnable_, no abstractions. The point is to learn something fast.
5. **Surface the state.** After every action (logic) or on every variant switch (UI), render the full relevant state so the user can see what changed.
6. **Capture the verdict where the next reader looks.** When the work has a GitHub issue, post the verdict as a comment on it: the question, the answer (which variant, which model, or "none of these"), and the reason. The next stage is often a fresh session that reads the issue thread and has never seen the file. With no issue, tell the user the verdict and where the file is. Fold any validated decision into the real code (a validated reducer lifts into the real module; a winning layout is rebuilt properly, not copied). The prototype file itself never lands on `main`: leave it uncommitted, or delete it once the verdict is recorded.

```bash
gh issue comment <number> --repo <owner/repo> --body-file /tmp/verdict.md
```
