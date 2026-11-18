# PR exam host — Spec

## Requirements

1. **Exam request.** The dispatcher has a route that accepts an exam request for a PR URL. It is
   reachable over Tailscale only and works whether babysit-pr calls it from the box or from the
   operator's Mac.
   - The request is idempotent per PR and head SHA: repeating it returns the same exam.
2. **Generation.**
   - A session using the `pr-exam` skill generates the exam on the box from the PR's diff, the PR
     body (including its merge-risk block), the repo's `GLOSSARY.md` if there is one, and the
     operator's learning records for that repo.
   - The response returns the exam URL.
   - Once generation starts, `exam` on the head commit is pending.
3. **The page**
   - shows the questions and accepts the answers;
   - never contains the answer key or rubrics, in either the HTML or any response the page
     fetches;
   - shows grading results only after the submission has been graded.
4. **Grading.**
   - A submission is graded on the server: multiple choice against the key, free text by a fresh
     grading session against the rubric fixed at generation time.
   - The grading session's provider is chosen by the dispatcher's usual provider priority.
   - The box's GitHub App posts every `exam` status. Nothing posts it with a personal token.
   - The pass rule comes from `pr-exam`: at least 80%, and every *why* question passed.
5. **Status after grading.**
   - **pass:** `exam` = success on the exam's head SHA.
   - **fail:** `exam` stays pending. The page shows the explanations, each quoting a diff hunk,
     and offers a follow-up attempt that covers only the missed concepts.
   - There is no limit on attempts.
6. **Notification.** The operator gets a Telegram message with the exam link when an exam is
   ready and when its result is in.
7. **Learning records** live on the box, one directory per repo, in the shape of `teach`'s
   learning records. Grading writes a record:
   - for each concept passed: demonstrated;
   - for each concept failed: misconception.
   Generation reads them (`pr-exam` requirement 8).
8. **New pushes.** A request for the same PR with a new head SHA:
   - **open exam:** replaces it with a new exam on the new head;
   - **passed exam:** runs a delta judgment.
     - **new concept:** a follow-up exam on the new concepts only, with `exam` pending on the new
       head.
     - **otherwise:** `exam` = success is posted on the new head.
9. **Bypass.** A bypass request (`skip` or `defer`) is accepted only if the label's most recent
   `labeled` event was made by the operator: the GitHub login configured on the box. In these
   requirements and scenarios, "the repo owner" means that login, also for a repo owned by an
   organization. The box checks the actor itself rather than
   trusting the caller. For an accepted request:
   - **`skip`:** posts success "bypassed" and generates nothing.
   - **`defer`:** posts success "deferred" and creates an exam that counts as debt.
10. **Exam debt.**
    - Deferred exams, and exams for PRs merged without a pass or bypass, stay open after the merge.
      The console lists them as exam debt until they are passed.
    - Passing a debt exam writes learning records and posts no status.
11. **Cleanup.**
    - **PR merged with a pass:** the exam page and its data are deleted, and the learning records
      are kept.
    - **PR closed without merging:** the exam is deleted and no records are written.
12. **Daily sweep:**
    - deletes exams whose PR has closed;
    - turns PRs merged without a pass or bypass into exam debt.

## Scenarios

### Scenario: requesting an exam returns its URL and a pending status

- **Given** an open PR `jesdi/general-skills#240` with head `abc123`
- **When** babysit-pr on the operator's Mac calls the exam route over Tailscale for that PR
- **Then** the response holds an exam URL, `exam` on `abc123` is pending, and the operator gets
  a Telegram message with the link

### Scenario: repeating a request doesn't create a second exam

- **Given** an exam already exists for `#240` at `abc123`
- **When** the same request comes again
- **Then** the same URL is returned, and no second generation session runs

### Scenario: the exam can't be reached from outside Tailscale

- **Given** an exam URL
- **When** it is requested from a non-Tailscale address
- **Then** the request is refused

### Scenario: the page never exposes the answers

- **Given** a generated exam with 3 multiple-choice and 2 free-text questions
- **When** the page and every response it fetches before submission are inspected
- **Then** none of them contains a correct-option marker or rubric text

### Scenario: a passing submission posts success

- **Given** the exam for `abc123`
- **When** the operator submits answers scoring 90%, with every *why* question passed
- **Then** `exam` = success on `abc123`, one demonstrated record per concept is written, and the
  page shows the result

### Scenario: a failing submission keeps the status pending and offers a follow-up

- **Given** the exam for `abc123`
- **When** the operator scores 70%, missing the concepts "lease ownership" and "base SHA recheck"
- **Then** `exam` stays pending, two misconception records are written, and the page shows both
  explanations with their diff hunks and a 2-question follow-up attempt

### Scenario: free text is graded by a fresh session against the fixed rubric

- **Given** an exam with one free-text question whose rubric was stored at generation time
- **When** the operator submits
- **Then** a new grading session is started, given the diff, the question, the rubric and the
  answer, and given no generation transcript

### Scenario: a push while the exam is open replaces it

- **Given** an open exam on `abc123`
- **When** a request arrives for the same PR at `def456`
- **Then** the old exam is replaced, and `exam` on `def456` is pending

### Scenario: a push after a pass with no new concept

- **Given** a passed exam on `abc123`
- **When** a request arrives for `def456` and the delta judgment finds no new concept
- **Then** `exam` = success is posted on `def456` and no new exam is created

### Scenario: a push after a pass with a new concept

- **Given** a passed exam on `abc123`
- **When** a request arrives for `def456` and the delta judgment finds "retry budget"
- **Then** a follow-up exam on "retry budget" only is created, and `exam` on `def456` is pending

### Scenario: the owner's skip is honoured

- **Given** the repo owner added `exam:skip` to `#240`
- **When** a skip bypass request arrives for `#240`
- **Then** `exam` = success, "bypassed", and no exam is generated

### Scenario: skip from a non-owner is refused

- **Given** `exam:skip` was added by `some-bot[bot]`
- **When** a skip bypass request arrives
- **Then** it is refused and `exam` is unchanged

### Scenario: a defer creates debt

- **Given** the repo owner added `exam:defer`
- **When** a defer bypass request arrives, and the PR is then merged
- **Then** `exam` = success, "deferred", and the exam stays open and is listed as debt in the
  console after the merge

### Scenario: a merge without a pass becomes debt

- **Given** private repo PR `jesdi/claude-config#12` merged by hand while `exam` was pending
- **When** the daily sweep runs
- **Then** that exam is listed as debt

### Scenario: passing a debt exam clears it

- **Given** a debt exam for merged `#240`
- **When** the operator passes it
- **Then** it leaves the debt list, records are written, and no status is posted

### Scenario: cleanup after a merge with a pass

- **Given** `#240` merged with `exam` = success
- **When** the merge is seen, by babysit-pr's merge or the sweep
- **Then** the exam page and data are deleted, the records remain, and the exam URL returns
  not-found

### Scenario: a PR closed without merging

- **Given** `#241` with an open exam is closed without merging
- **When** the sweep runs
- **Then** the exam is deleted and no records are written for it

## Out of scope

- Visual questions.
- Access from outside Tailscale.
- Records stored in the repo or in Engram.
- Reusing the review-page template.
- Treating a pass as merge approval.
- Keeping exam pages after a merge.
