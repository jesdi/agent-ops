# Sentry triage — Spec

## Requirements

1. **Opt-in config.**
   - A project can carry a `sentry:` block in `targets.yaml` that names the Sentry org and maps
     each Sentry project slug to one of the project's repositories.
   - A project without the block is never read from Sentry.
   - A block that maps to a repository the project does not have is a config error at load
     time, not at run time.
2. **Daily intake.**
   - Once a day, a managed job named `sentry` handles the unresolved Sentry issues still marked
     for review in each mapped Sentry project.
   - It runs on its own timer and can be paused, resumed and run now from the console's
     Services page, independently of the triage sweep.
   - Each run handles at most 20 issues per Sentry project, the most frequent first. The rest
     wait for the next run.
3. **One verdict per issue.** An LLM session judges each issue and gives exactly one verdict:
   `bug`, `ignore` or `duplicate`.
   - The session reads Sentry itself through the Sentry MCP server, so it can open events and
     stack traces as it needs them.
   - Its Sentry access is read-only, and it has no GitHub write access.
   - A deterministic step carries out every write to GitHub and Sentry, as the triage sweep
     already does.
4. **`bug` verdict.** A new issue is filed in the mapped repository.
   - Labels: `bug` and `sentry-source`.
   - The body contains the Sentry permalink, the error type and message, the top in-app stack
     frames, the event count, the user count, the first-seen release and a marker carrying
     the Sentry issue id.
   - The GitHub issue URL is posted back on the Sentry issue as a note.
5. **`ignore` verdict.** The Sentry issue is archived until it escalates, and a note on it
   gives the reason. Nothing is filed on GitHub.
6. **`duplicate` verdict.** The session names an open GitHub issue in the mapped repository.
   - That issue gets a comment with the Sentry permalink and current counts.
   - The Sentry issue gets a note with the GitHub URL.
   - No new GitHub issue is filed.
   - A duplicate target that does not exist or is closed downgrades the verdict to `bug`.
7. **Idempotence.**
   - Every issue that is handled is marked reviewed in Sentry. A later run does not see it again.
   - The intake never files a second GitHub issue for a Sentry issue id whose marker already
     exists on an open or closed issue in the repository. This holds even if marking it
     reviewed failed.
8. **Escalation returns.** An archived issue that Sentry escalates comes back as for review
   and is judged again on the next run.
9. **Report.** Each run sends one operator message per project: counts per verdict, and a line
   for each filed or duplicate issue with its GitHub link. A run that cannot reach Sentry
   reports the failure and changes nothing.
10. **Labels exist.** `sentry-source`, `reproduced` and `not-reproduced` are created in every
    repository, the same way the existing taxonomy labels are.
11. **Reproduce stage.**
    - When an open issue labelled `sentry-source` that carries neither `reproduced` nor
      `not-reproduced` is claimed, its task enters a new pipeline stage, `reproduce`, before
      `spec`.
    - Every other issue enters `spec` exactly as today.
12. **Reproduce run.**
    - The `reproduce` session follows `diagnosing-bugs` against the repository and changes no
      product code.
    - It pushes no branch and opens no PR. Any local changes it made are discarded when the
      stage ends.
    - It posts one comment on the issue. On success the comment gives numbered reproduction
      steps, the command or test that triggers the bug and the observed output. On failure it
      lists what was tried and why each attempt did not trigger the bug.
    - It adds exactly one of `reproduced` or `not-reproduced`.
13. **Reproduced continues.** With `reproduced`, the same task moves on to `spec`, and the
    spec session reads the reproduction comment as its starting point.
14. **Not reproduced closes.**
    - With `not-reproduced`, the issue is closed as not planned, with the attempts comment.
    - The task ends as done, not failed.
15. **Daily not-reproduced digest.**
    - Once a day the operator gets one Telegram message listing every issue closed as
      `not-reproduced` since the previous digest. Each entry gives the repository, number, title, link and the
      Sentry permalink.
    - A day with no such issue sends no message.
16. **Never retried.**
    - An issue that carries `reproduced` or `not-reproduced` never enters `reproduce` again.
    - When the operator reopens it and removes the label, the next claim runs `reproduce` once
      more.
17. **Interrupted run.**
    - A `reproduce` session that ends without an outcome adds no label and no comment.
    - It gets the pipeline's usual handling of a stage that ends without an outcome. That
      covers resume, the loop caps and parking for the operator.
18. **Models, capacity and usage.**
    - Intake uses `models.triage`.
    - `reproduce` uses a new top-level `models.reproduce` list. A project's own `models:` block
      may override it, like any stage.
    - Intake counts against capacity like the triage sweep, and `reproduce` like any task stage.
    - Neither starts unless the usage gate admits its model.

## Scenarios

### Scenario: project without a sentry block is skipped
- **Given** project `alpha` has no `sentry:` block
- **When** the 04:00 intake runs
- **Then** no Sentry API call is made for `alpha` and the report omits it

### Scenario: sentry block mapping to an unknown repository is rejected
- **Given** project `pe` has repositories `portfolio_evaluator` only, and its `sentry:` block
  maps `pe-frontend` to `pe-web`
- **When** the dispatcher loads `targets.yaml`
- **Then** loading fails with an error naming `pe-web` and project `pe`

### Scenario: real bug is filed
- **Given** Sentry issue `PE-FRONTEND-3A` is unresolved and for review: `TypeError: Cannot read properties of undefined (reading 'price')` in `HoldingRow.tsx`, 41 events, 9 users, first seen in release `sha-0fd0461e`
- **When** the intake runs and the session's verdict is `bug`
- **Then** one issue is opened in `jesdi/portfolio_evaluator` labelled `bug` and `sentry-source`, with the permalink, message, top in-app frames, `41` events, `9` users, release `sha-0fd0461e` and the marker for `PE-FRONTEND-3A`
- **And** `PE-FRONTEND-3A` gets a note with the new issue URL and is marked reviewed

### Scenario: noise is archived
- **Given** Sentry issue `PE-FRONTEND-3B` is `Error: ResizeObserver loop completed with undelivered notifications` from a `chrome-extension://` frame
- **When** the session's verdict is `ignore` with reason "browser extension"
- **Then** `PE-FRONTEND-3B` is archived until escalating with a note "browser extension", is marked reviewed, and no GitHub issue is opened

### Scenario: duplicate of an open issue
- **Given** open issue `#640` already describes the `HoldingRow` price crash, and Sentry issue `PE-FRONTEND-3C` is the same error from a later release
- **When** the session's verdict is `duplicate` of `#640`
- **Then** `#640` gets one comment with the permalink of `PE-FRONTEND-3C` and its counts, `PE-FRONTEND-3C` gets a note linking `#640` and is marked reviewed, and no issue is opened

### Scenario: duplicate target is closed
- **Given** the session names `#512` as the duplicate target and `#512` is closed
- **When** the verdict is applied
- **Then** it is applied as `bug`: a new labelled issue is filed, and the report flags the downgrade

### Scenario: rerun after a failed "mark reviewed"
- **Given** the previous run filed issue `#655` for `PE-FRONTEND-3A`, but marking it reviewed in Sentry failed, so it is still for review
- **When** the next intake runs
- **Then** no second issue is opened for `PE-FRONTEND-3A`, and it is marked reviewed with a note linking `#655`

### Scenario: handled issue is not seen again
- **Given** `PE-FRONTEND-3A` was marked reviewed yesterday and has had no escalation since
- **When** today's intake runs
- **Then** `PE-FRONTEND-3A` is not sent to the session

### Scenario: archived issue escalates
- **Given** `PE-FRONTEND-3B` was archived until escalating, and Sentry escalates it after a spike from 3 to 400 events per hour
- **When** the next intake runs
- **Then** `PE-FRONTEND-3B` is sent to the session again and gets a fresh verdict

### Scenario: Sentry unreachable
- **Given** the Sentry API returns `401` for org `jesdi`
- **When** the intake runs
- **Then** nothing is written to GitHub or Sentry, and the operator report says the intake for `pe` failed with `401`

### Scenario: labels are created
- **Given** repository `jesdi/portfolio_evaluator` has no `sentry-source` label
- **When** the dispatcher reconciles labels on its next pass
- **Then** `sentry-source`, `reproduced` and `not-reproduced` exist on the repository

### Scenario: unreproduced sentry bug enters reproduce
- **Given** issue `#655` is labelled `bug`, `sentry-source` and `auto`, its board Status is Ready, and it has neither `reproduced` nor `not-reproduced`
- **When** the claim round claims `#655`
- **Then** its task enters stage `reproduce`, not `spec`

### Scenario: issue without sentry-source is unaffected
- **Given** issue `#656` is labelled `bug` and `auto`, Ready, without `sentry-source`
- **When** the claim round claims `#656`
- **Then** its task enters `spec` as today

### Scenario: reproduce succeeds and the task moves to spec
- **Given** `#655` is in stage `reproduce`
- **When** the session follows `diagnosing-bugs`, triggers the `HoldingRow` crash by loading `/portfolio/1` with a holding that has no quote, and reports outcome `reproduced`
- **Then** `#655` gets one comment with numbered steps, the triggering command and the observed `TypeError`, and the label `reproduced`
- **And** the same task moves to `spec`, whose prompt points the session at the reproduction comment
- **And** no branch was pushed and no PR was opened for `#655`

### Scenario: reproduce fails and the issue is closed
- **Given** `#657` (`sentry-source`, an error seen only in Safari 15) is in stage `reproduce`
- **When** the session reports outcome `not-reproduced` after its attempts
- **Then** `#657` gets one comment listing each attempt and why it failed, the label `not-reproduced`, and is closed as not planned
- **And** its task ends as done

### Scenario: daily digest lists not-reproduced issues
- **Given** `#657` and `#661` were closed as `not-reproduced` since yesterday's digest
- **When** the daily digest is due
- **Then** the operator gets one Telegram message listing both, each with repository, number, title, issue link and Sentry permalink

### Scenario: no digest on a quiet day
- **Given** no issue was closed as `not-reproduced` since the previous digest
- **When** the daily digest is due
- **Then** no message is sent

### Scenario: outcome label stops another reproduce run
- **Given** `#655` carries `reproduced` and its task was flushed after an earlier failure
- **When** a new claim of `#655` happens
- **Then** the task enters `spec`, not `reproduce`

### Scenario: operator re-arms reproduction
- **Given** `#657` is closed with `not-reproduced`
- **When** the operator reopens it and removes the label, and the claim round claims it
- **Then** its task enters `reproduce` once more

### Scenario: reproduce session ends without an outcome
- **Given** the `reproduce` session for `#658` ends its turn without reporting an outcome
- **When** the dispatcher handles the stage
- **Then** `#658` gets no label and no comment, and the stage is resumed or parked by the usual loop rules

### Scenario: intake cap
- **Given** Sentry project `pe-frontend` has 35 unresolved for-review issues
- **When** the intake runs
- **Then** the 20 with the most events are judged and the other 15 stay for review for the next run

### Scenario: intake session cannot write
- **Given** the intake session runs with the Sentry MCP
- **When** it tries to archive, resolve or comment on a Sentry issue, or to write to GitHub
- **Then** the write is refused, and only the deterministic apply step changes Sentry or GitHub

### Scenario: usage gate denies the model
- **Given** no entry of `models.triage` (intake) or `models.reproduce` (reproduce) is admitted by the usage gate
- **When** intake or a reproduce claim is due
- **Then** it does not start, nothing is written, and it is retried on a later pass

## Out of scope

- Fixing frontend Sentry delivery in portfolio_evaluator.
- Sentry webhooks, Sentry alert rules that open GitHub issues, and Seer.
- Resolving Sentry issues when a fix merges.
- Performance issues and transactions.
- Error tracking providers other than Sentry.
- A console page for Sentry triage.
