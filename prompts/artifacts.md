## Persistent review artifacts

Keep review material accessible on the task webpage across all sessions.
Automatically register prototypes, diagrams, questionnaires with their answers,
specs, and other outputs used for human review or to guide later work. Do not
ask whether to register each file. Exclude scratch files, logs, credentials,
and temporary outputs.

Maintain `.agent/artifacts.json` as an array, preserving existing entries:

```json
[
  {"id": "spec", "name": "Specification", "path": "docs/specs/task-design.md"},
  {"id": "prototype", "name": "Task page prototype", "path": ".agent/prototype.html"},
  {"id": "review-answers", "name": "Review questions and answers", "path": "docs/review/answers.md"}
]
```

Use descriptive names and stable lowercase IDs (letters, digits, hyphens,
underscores). Revisions update the same file and ID; never remove earlier
sessions' entries. Paths must name individual files inside this task's
worktree, maximum 20 MiB each. Write the manifest atomically (temporary file,
then rename), immediately after producing or revising an artifact and BEFORE
signalling a stage boundary or waiting for human input. The dispatcher collects
it on each pass. The manifest is local session state; do not commit it.

Commit and push Markdown review artifacts to the task branch before registering
them so they can open on GitHub. Collection verifies publication; it does not
commit files on your behalf. Keep them outside ignored `.agent/` paths (for example,
`docs/review/`). For an existing questionnaire under `.agent/`, retain the
signal file there and maintain a review copy under `docs/review/` with the
questions AND answers. Never register a Markdown file you cannot publish.

HTML prototypes must be self-contained: inline CSS, JavaScript, and image data;
no sibling files, external scripts, API calls, or credentials. They open rendered
in an isolated browser context. Other file types open as a preview or download.
Read the existing artifact manifest at the start of later sessions to recover
review context. Preserve these files when revising the solution.
