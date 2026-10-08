## Persistent review artifacts

Keep review material accessible on the task webpage across all sessions.
Automatically register prototypes, diagrams, questionnaires with their answers,
specs, and other outputs used for human review or to guide later work. Do not
ask whether to register each file. Exclude scratch files, logs, credentials,
and temporary outputs.

Maintain `.agent/artifacts.json` as an array, preserving existing entries:

```json
[
  {"id": "spec", "name": "Specification", "path": "specs/2026-10-12-task/spec.md"},
  {"id": "prototype", "name": "Task page prototype", "path": ".agent/prototype.html"},
  {"id": "plan-review", "name": "Plan review page", "path": ".agent/review.html"}
]
```

Use descriptive names and stable lowercase IDs (letters, digits, hyphens,
underscores). Revisions update the same file and ID; never remove earlier
sessions' entries. Paths must name individual files inside this task's
worktree, maximum 20 MiB each. Write the manifest atomically (temporary file,
then rename), immediately after producing or revising an artifact and BEFORE
signalling a stage boundary or waiting for human input. The dispatcher collects
it on each pass. The manifest is local session state; do not commit it.

A Markdown artifact opens on GitHub only when its file is on the pushed task
branch as registered; collection verifies that and never adds a file to the
branch for you. Of the review material, only the spec folder belongs on the
branch. Questionnaires, their answers and other review notes stay under the
ignored `.agent/` directory: register them from there, and the task webpage
shows the dispatcher's own snapshot of each. Never add a second version of
them to the branch.

HTML prototypes must be self-contained: inline CSS, JavaScript, and image data;
no sibling files, external scripts, API calls, or credentials. They open rendered
in an isolated browser context. Other file types open as a preview or download.
Read the existing artifact manifest at the start of later sessions to recover
review context. Preserve these files when revising the solution.

## What `specs/` is

`specs/` is a change log: each folder records one past change as it was
decided then, and nothing keeps it up to date afterwards. The code and
`CONTEXT.md` are the present state. Where a spec under `specs/` and the code
disagree, the code wins: work against the code, and read an older spec only
to learn why a change was made. The spec of your own task is the one that
says what to build now.
