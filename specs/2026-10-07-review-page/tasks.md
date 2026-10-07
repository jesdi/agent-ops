# Review page: one interactive page for the plan gate and the questionnaire — Tasks

Each task links to the goal in proposal.md. Work the frontier (tasks whose blockers are done);
commit per task with its ID. Every task ends with `make gate` green.

- [ ] **T1** — The gate accepts a review page by shape; the Markdown summary and its parser go;
  the fixture page; `prompts/plan.md` step 5 asks for the page from `review-page` in plan mode.
  _Goal: the plan session's output is the interactive page the operator reviews._
  Seam: `dispatcher.main` pass with a stage signal. Blocked by: none.
- [ ] **T2** — The `review-page` skill in `jesdi/general-skills` (`SKILL.md`, `template.html`
  from the accepted prototype with the marker and the fixed script, `schema.md`), its own pull
  request and release, and the `jesdi/portfolio_eval` pin.
  _Goal: the page has one source and the session fills only content._
  Seam: `jesdi/general-skills` skill `review-page`. Blocked by: none.
- [ ] **T3** — The dispatcher writes the answers file from an `answers` intent: schema, atomic
  write through the `.agent/` helper, newest-wins per task per pass, the drop and fail rules,
  the `answers` request's page fingerprint, the two wake messages.
  _Goal: every selection is saved as a file the sessions read, and a submission resumes the session._
  Seam: `dispatcher.main._apply_intents`. Blocked by: T1.
- [ ] **T4** — The console API: `POST .../answers` with validation and 202; `GET .../request`
  extended with `revision` and `answers` (worktree file overlaid with the newest pending intent).
  _Goal: the console can send a selection and give saved selections back._
  Seam: the two web routes. Blocked by: T3.
- [ ] **T5** — Answers files are review artifacts: automatic registration of `review-answers`
  and `questionnaire-answers`; `RETENTION_DAYS = 7`.
  _Goal: the record outlives the worktree for a week and is visible with the task._
  Seam: `dispatcher.task_artifacts.collect` and `cleanup`. Blocked by: none.
- [ ] **T6** — The review route and the bridge: `/task/:target/:issue/review` full screen;
  source and version checks; `ready` then `restore`; debounce with flush on button and
  `pagehide`; reload with notice on a revision change; the task page link; no "approve plan"
  button. Phone width and both themes in e2e.
  _Goal: the operator reviews and answers on one interactive page from any device._
  Seam: console route `/task/:target/:issue/review`. Blocked by: T2, T4.
- [ ] **T7** — The prompts and the pin: spec, plan, implement and review prompts read and write
  the one record; `.my-skills.json` pins `review-page`; fixture synced; `CONTEXT.md`, `README.md`.
  _Goal: every session in the pipeline reads and writes the same record._
  Seam: `prompts/*.md` rendered by the dispatcher. Blocked by: T1, T2.

The ticket files under `.agent/tickets/01` to `07` carry the same graph with the acceptance
criteria.
