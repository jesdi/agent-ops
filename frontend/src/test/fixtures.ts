import type {
  BoardView, BudgetView, FailuresView, HistoryView, PendingIntentsView,
  TaskCard, TaskDetail,
} from '../lib/api'

export const parkedCard: TaskCard = {
  issue: 42, target: 'widget', title: 'Fix login redirect',
  stage: 'implement', park: 'question', column: 'parked', slot: 1,
  branch: 'fix/login-redirect', model: 'sonnet', park_note_pending: true,
  park_note: 'Should I use the staging redirect URL or prod?', feedback_pending: false,
  updated_at: '2026-07-25T10:00:00Z', attached: false, consuming_capacity: false,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null, score: null,
  undelivered_messages: 0, wake_blocked: false,
}

export const reviewCard: TaskCard = {
  issue: 44, target: 'widget', title: 'Add search feature',
  stage: 'awaiting-spec-review', park: 'awaiting-review', column: 'needs-review',
  slot: -1,
  branch: 'feat/search', model: 'opus', park_note_pending: false,
  park_note: 'spec ready for review', feedback_pending: false,
  updated_at: '2026-07-25T09:00:00Z', attached: false, consuming_capacity: false,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null, score: null,
  undelivered_messages: 0, wake_blocked: false,
}

export const inProgressCard: TaskCard = {
  issue: 41, target: 'widget', title: 'Add CSV export',
  stage: 'implement', park: '', column: 'in-progress', slot: 2,
  branch: 'feat/csv-export', model: 'opus', park_note_pending: false,
  park_note: '', feedback_pending: false,
  updated_at: '2026-07-25T11:30:00Z', attached: false, consuming_capacity: true,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null, score: null,
  undelivered_messages: 0, wake_blocked: false,
}

// The motivating pair: both sit in the Parked column, only one holds a unit.
export const loginParkedCard: TaskCard = {
  issue: 45, target: 'widget', title: 'Rate-limit webhooks',
  stage: 'spec', park: 'parked-login', column: 'parked', slot: 0,
  branch: 'feat/rate-limit', model: 'opus', park_note_pending: false,
  park_note: '', feedback_pending: false,
  updated_at: '2026-07-25T11:00:00Z', attached: false, consuming_capacity: true,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null, score: null,
  undelivered_messages: 0, wake_blocked: false,
}

export const ciParkedCard: TaskCard = {
  issue: 46, target: 'widget', title: 'Fix nightly digest',
  stage: 'implement', park: 'awaiting-ci', column: 'awaiting-ci', slot: 1,
  branch: 'fix/nightly-digest', model: 'sonnet', park_note_pending: false,
  park_note: '', feedback_pending: false,
  updated_at: '2026-07-25T10:30:00Z', attached: false, consuming_capacity: false,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null, score: null,
  undelivered_messages: 0, wake_blocked: false,
}

export const board: BoardView = {
  columns: [
    { key: 'queued', title: 'Queued', cards: [] },
    { key: 'in-progress', title: 'In progress', cards: [inProgressCard] },
    { key: 'needs-review', title: 'Needs review', cards: [] },
    { key: 'pr-open', title: 'PR review', cards: [] },
    { key: 'done', title: 'Done', cards: [] },
    { key: 'parked', title: 'Parked', cards: [parkedCard, loginParkedCard] },
    { key: 'awaiting-ci', title: 'Awaiting CI', cards: [ciParkedCard] },
    { key: 'resuming', title: 'Resuming', cards: [] },
    { key: 'stalled', title: 'Stalled', cards: [] },
    { key: 'failed', title: 'Failed', cards: [] },
    { key: 'wont-do', title: 'Wont do', cards: [] },
  ],
  capacity: { active: 2, capacity: 3, slots_used: 4, max_slots: 3, slots_held: [1, 2] },
  upcoming: [], upcoming_stale: false, median_cycle_seconds: null,
  next_claim: { verdict: 'no-candidates', next_pass_eta: '2026-07-25T12:05:00Z', next_issue: 0, next_target: '', minutes_to_reset: 0 },
}

export const budget: BudgetView = {
  utilization: 0.62, minutes_to_reset: 95, source: 'oauth',
  would_spawn: true, threshold_applied: 'base',
}

export const budgetUnavailable: BudgetView = {
  utilization: 0, minutes_to_reset: 0, source: 'unavailable',
  would_spawn: false, threshold_applied: 'n/a',
}

export const taskDetail: TaskDetail = {
  card: parkedCard,
  pane_tail: '? Should I use the staging redirect URL or prod?\n> ',
  session_alive: true,
  worktree: '/home/agent/worktrees/task-42',
  messages: [],
  delivery_contract: 'will deliver when the session resumes',
  ci_run_id: 0,
  effort: 3,
  labels: ['auto'],
  timeline: [],
}

export const failures: FailuresView = {
  quarantined: [{
    target: 'widget', task_issue: 38, blocker_repo: 'jesdi/widget',
    blocker_issue: 39, fingerprint: 'pytest::test_auth_flow',
    created_at: '2026-07-24T22:10:00Z', blocker_open: true,
  }],
  fingerprints: [{
    fingerprint: 'pytest::test_auth_flow', repo: 'jesdi/widget',
    issue: 39, when: '2026-07-24T22:10:00Z',
  }],
}

export const history: HistoryView = {
  events: [
    { ts: '2026-07-25T11:30:00Z', event: 'stage-started', target: 'widget',
      issue: 41, stage: 'implement', model: 'opus', actor: 'dispatcher', detail: '' },
    { ts: '2026-07-25T10:00:00Z', event: 'parked', target: 'widget',
      issue: 42, stage: 'implement', model: 'sonnet', actor: 'dispatcher',
      detail: 'question' },
  ],
}

export const noPendingIntents: PendingIntentsView = { intents: [] }
export const pendingReplyIntent: PendingIntentsView = {
  intents: [{ action: 'reply', target: 'widget', issue: 42, actor: 'dev@localhost',
              created_at: '2026-07-25T11:58:00Z' }],
}
