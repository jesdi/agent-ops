// The exam view as GET /api/exam/jesdi/general-skills/240 serves it, for the exam page tests.
export const SHA = 'a'.repeat(40)
export const REPO = 'jesdi/general-skills'

type Fields = object

type Chat = { tips: number; tips_left: number | null; assisted: boolean; thread: unknown[] }

/** A question of the open attempt: no tip, no thread, every tip left. */
export const question = <T extends Fields>(q: T): Chat & T => ({ tips: 0, tips_left: 3, assisted: false, thread: [], ...q })

/** A question of a submitted attempt: it takes no tip. */
export const keptQuestion = <T extends Fields>(q: T): Chat & T => question({ ...q, tips_left: null })

/** The open attempt, open to chat. */
export const openExam = (questions: Fields[], more: Fields = {}) =>
  ({ version: 1, repo: REPO, pr: 240, head_sha: SHA, questions, general: [], chat: true, ...more })

/** A submitted attempt of this head, open to chat, with its result once graded. */
export const keptAttempt = (attempt: number, questions: Fields[], result: Fields | null, more: Fields = {}) =>
  ({ attempt, questions, general: [], chat: true, result, ...more })

/** The exam view: attempt 1 open with `questions`, unless `more` says otherwise. */
export const examView = (questions: Fields[], more: Fields = {}) => ({
  repo: REPO, pr: 240, state: 'open', head_sha: SHA, debt: false, detail: 'attempt 1', attempt: 1, bypassed: null,
  tip_cap: 3, exam: openExam(questions), attempts: [], ...more,
})
