# PR exam host assist — Spec

Extends `specs/pr-exam-host`. The rules come from the skills spec `specs/pr-exam-assist`
(`pr_exam.py context`, `score --flags`, `record`). Where this spec is silent,
`specs/pr-exam-host` holds.

## Requirements

1. **Turns.** The console has a route that takes one turn on an exam: a tip, a message in a
   question's thread, or a message in the general chat. It streams the reply to the page as it
   is written.
   - It is guarded like every console route: Tailscale only.
   - It builds nothing itself. The prompt is what `pr_exam.py context` prints for the turn.
   - A turn's reply may contain the answer key, as the chat rules allow. This narrows
     `pr-exam-host` requirement 3: the page and every response it fetches still contain no key
     or rubric, except the replies to turns the operator asked for.
2. **Where a turn runs.** The console runs each turn itself, as a background task it owns.
   - The model runs in the same sandbox as the onboarding inspection: a one-shot container
     with no tools, all capabilities dropped, a throwaway home and only the auth file mounted.
     The prompt arrives on stdin and the reply streams out.
   - A turn belongs to the console, not the page. When the page closes or disconnects
     mid-reply, the reply still finishes and is stored. It counts (requirements 5 and 6), and a
     reload shows it.
   - When the console restarts mid-turn, that turn is shown as failed, and it counts for
     nothing.
   - There is no new unit and no new socket. The console already runs as the user that can read
     the Claude credential; isolating it is a separate issue.
3. **Model and limits.**
   - A turn uses the `models.triage` entry and the usage gate.
   - When usage doesn't admit a call, the turn fails at once with "unavailable: usage limit".
   - When `context` refuses the prompt as too long, the turn fails with "PR too large for chat".
   - Either way, nothing is recorded.
4. **Threads.** Every tip, message and reply is kept per attempt in the exam directory.
   - Threads survive a page reload.
   - They are deleted with the exam.
   - They are never given to the grading session.
5. **Tip cap.**
   - The host refuses a fourth tip on a question of an attempt.
   - A tip counts only once its reply has finished.
   - Tips are allowed only while the attempt is open.
6. **Assisted.** While an attempt is open, a question becomes assisted in two cases: a tip on
   it finishes, or a reply in its thread finishes. The general chat marks nothing. Once the
   attempt is submitted, nothing marks it.
7. **Bypass.**
   - While an attempt is open, the operator can bypass one question, after confirming it.
   - A bypass can be undone until submit.
   - The host refuses a second bypass in the same attempt.
   - Submit accepts a bypassed question without an answer.
8. **Flags at submit.** Submit writes the attempt's bypassed and assisted questions once. The
   grading uses that snapshot (`score --flags`). A turn that finishes after submit changes
   neither the score nor the records.
9. **Attempts are kept.** Each attempt keeps its own exam, flags, answers, grades, result and
   threads until the exam is deleted.
   - A turn about an attempt is built from that attempt's files, never from a later attempt's.
   - After submit, a question's thread gets the submitted answer. Once graded, it also gets
     the question's points and, for a free-text question, the grader's feedback (`context`
     takes them).
10. **What is available in each state.**

    | Attempt | Tips | Question threads | General chat | Bypass |
    |---|---|---|---|---|
    | the open attempt | ✓ (cap 3) | ✓ | ✓ | ✓ (1) |
    | grading | — | ✓ | ✓ | — |
    | passed | — | ✓ | ✓ | — |
    | failed (while its follow-up is generated, and after it opens) | — | ✓ | ✓ | — |
    | none yet: the first generation, skipped, disabled, error | — | — | — | — |

11. **The result.**
    - A bypassed question is shown as bypassed, with its explanation and diff hunk, and counts
      in neither the points nor the maximum.
    - An assisted question is marked as assisted.
    - Each missed or partial free-text question shows the grader's one-line feedback.
12. **The page.**
    - Each question has its actions: Tip with a counter (n / 3), Ask, and Bypass. Its thread
      sits inline under it.
    - The general chat is a drawer on the right, and a bottom sheet on a narrow screen.
    - Selecting text inside a question or its result pre-fills that question's thread with the
      quote. Selecting it anywhere else pre-fills the general chat.
    - A reply shows while it streams.
    - A failed turn shows its error and a retry.
13. **The skills pin** moves to the first skills release with `pr-exam-assist`. The host docs
    say it must never move back past that release: an older `pr_exam.py` can't read an
    `assisted` record.

## Scenarios

Unless a scenario says otherwise, the exam is attempt 1 on `jesdi/general-skills#240` at
`abc123`. It has 6 questions:
- `q1`–`q3`: multiple choice;
- `q4`: free text, on a *why* concept;
- `q5` and `q6`: free text.

### Scenario: a tip streams in and counts

- **Given** the exam is open and `q5` has had no tip
- **When** the operator asks for a tip on `q5` with the draft "the token only grows"
- **Then** the reply streams to the page as it is written. Once it finishes, `q5` shows
  "Tip 1 / 3" and is assisted, and after a reload the tip is still under `q5`.

### Scenario: closing the page mid-reply keeps the reply

- **Given** `q5` has had 1 tip
- **When** the operator asks for tip 2 on `q5` and closes the tab before the reply finishes
- **Then** the reply still finishes. After a reopen, `q5` shows "Tip 2 / 3" with the full
  tip, and `q5` is assisted.

### Scenario: a fourth tip is refused

- **Given** `q5` has had 3 tips in this attempt
- **When** the operator asks for another
- **Then** the host refuses it, no model call runs, and the tip button on `q5` is disabled

### Scenario: a tip that fails doesn't count

- **Given** `q5` has had 1 tip
- **When** a tip request on `q5` fails mid-stream
- **Then** `q5` still shows "Tip 1 / 3", the page shows the error with a retry, and `q5`'s
  assisted state is what it was before

### Scenario: a question thread marks the question assisted

- **Given** `q2` has had no tip and no message
- **When** the operator asks "is option 1 right?" in `q2`'s thread and the reply finishes
- **Then** `q2` is assisted

### Scenario: the general chat marks nothing

- **When** the operator asks "what is the riskiest change?" in the general chat and the reply
  finishes
- **Then** no question is assisted

### Scenario: usage refuses a turn

- **Given** the usage gate doesn't admit the `models.triage` entry
- **When** the operator asks for a tip on `q5`
- **Then** the page shows "unavailable: usage limit" at once, no model call runs, and `q5`'s
  tip count is unchanged

### Scenario: a PR too large for chat

- **Given** a PR whose diff makes `context` refuse the prompt as too long
- **When** the operator sends any turn
- **Then** the page shows "PR too large for chat", and no model call runs

### Scenario: the model runs sandboxed with no tools

- **When** the operator asks for a tip on `q5`
- **Then** the model command is a one-shot container with all capabilities dropped, a
  throwaway home, only the auth file mounted and no tools, and it gets the `context` prompt on
  stdin

### Scenario: a console restart mid-turn

- **Given** a tip on `q5` is streaming, and `q5` has had 1 tip
- **When** the console restarts before the reply finishes
- **Then** after the restart the page shows that tip as failed with a retry, `q5` still shows
  "Tip 1 / 3", and `q5`'s assisted state is what it was before

### Scenario: bypass, undo, and a second bypass

- **Given** the exam is open
- **When** the operator bypasses `q6` and confirms, then tries to bypass `q2`
- **Then** the host refuses the second bypass. After the operator undoes `q6`'s bypass,
  bypassing `q2` is accepted.

### Scenario: submit with a bypassed question unanswered

- **Given** `q6` is bypassed and has no answer, and every other question has an answer
- **When** the operator submits
- **Then** the submit is accepted and the attempt moves to grading

### Scenario: submit with an unanswered question that isn't bypassed

- **Given** `q6` has no answer and is not bypassed
- **When** the operator submits
- **Then** the submit is refused, as today

### Scenario: flags are fixed at submit

- **Given** the operator submitted with `q2` assisted and `q6` bypassed
- **When** a reply in `q5`'s thread finishes while the attempt is grading
- **Then** grading scores with `q2` assisted and `q6` bypassed only, and `q5` gets no
  `assisted` record

### Scenario: a bypassed question in the result

- **Given** `q6` was bypassed, and every other question scored its full point
- **When** the result is shown
- **Then** the page shows 5 / 5, a pass, and `q6` marked as bypassed with its explanation and
  hunk

### Scenario: the grader's feedback on the result

- **Given** `q5` was graded partial with the feedback "misses that the token only grows"
- **When** the result is shown
- **Then** `q5` shows that feedback under its explanation

### Scenario: asking why after a fail uses the failed attempt

- **Given** attempt 1 failed with `q5` answered "the token is random", scored 0, with
  feedback "misses that the token only grows", and the follow-up attempt 2 is open with its
  own `q5`
- **When** the operator asks "why was I wrong?" in attempt 1's `q5` thread
- **Then** the turn is built from attempt 1's exam, answer, points and feedback, not from
  attempt 2's `q5`. It marks nothing in either attempt.

### Scenario: no tips once submitted

- **Given** the attempt is grading
- **When** the operator asks for a tip on `q5`
- **Then** the host refuses it, and the page shows no tip button

### Scenario: chat while grading

- **Given** the attempt is grading
- **When** the operator asks a question in `q5`'s thread
- **Then** the reply streams in, and the prompt has `q5`'s submitted answer and no points

### Scenario: threads never reach the grader

- **Given** `q5` has 2 tips and a thread of 3 messages
- **When** the attempt is graded
- **Then** the grading session's inputs contain no tip and no thread message

### Scenario: threads go with the exam

- **Given** an exam with threads on 2 questions
- **When** the PR merges with a pass and the exam is deleted
- **Then** its threads are gone, and its learning records are kept

### Scenario: a selection pre-fills the right thread

- **When** the operator selects "the head SHA" in `q1`'s prompt
- **Then** `q1`'s thread opens with the quote pre-filled
- **When** the operator selects text in the page header
- **Then** the general chat opens with that quote pre-filled

### Scenario: the general chat on a narrow screen

- **Given** a 390 px wide window
- **When** the operator opens the general chat
- **Then** it shows as a bottom sheet, and the page has no horizontal scroll

## Out of scope

- Grading one question at a time.
- A cap on chat messages.
- Telegram for tips and chat.
- Exam rules in the host: tip and chat content, scoring and records come from `pr_exam.py`.
- Changes to generation, delta judgment, bypass labels, debt and the sweep.
