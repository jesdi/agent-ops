import type {
  BoardView, BudgetView, FailuresView, HistoryView, PendingIntentsView,
  QueueView, TaskCard, TaskDetail,
} from '../lib/api'

export const parkedCard: TaskCard = {
  issue: 42, target: 'jesdi/widget', title: 'Fix login redirect',
  stage: 'implement', park: 'question', column: 'parked', slot: 1,
  branch: 'fix/login-redirect', model: 'sonnet', park_note_pending: true,
  park_note: 'Should I use the staging redirect URL or prod?', feedback_pending: false,
  updated_at: '2026-07-25T10:00:00Z', attached: false, consuming_capacity: false,
}

export const reviewCard: TaskCard = {
  issue: 44, target: 'jesdi/widget', title: 'Add search feature',
  stage: 'awaiting-spec-review', park: 'awaiting-review', column: 'needs-review',
  slot: -1,
  branch: 'feat/search', model: 'opus', park_note_pending: false,
  park_note: 'spec ready for review', feedback_pending: false,
  updated_at: '2026-07-25T09:00:00Z', attached: false, consuming_capacity: false,
}

export const inProgressCard: TaskCard = {
  issue: 41, target: 'jesdi/widget', title: 'Add CSV export',
  stage: 'implement', park: '', column: 'in-progress', slot: 2,
  branch: 'feat/csv-export', model: 'opus', park_note_pending: false,
  park_note: '', feedback_pending: false,
  updated_at: '2026-07-25T11:30:00Z', attached: false, consuming_capacity: true,
}

// The motivating pair: both sit in the Parked column, only one holds a unit.
export const loginParkedCard: TaskCard = {
  issue: 45, target: 'jesdi/widget', title: 'Rate-limit webhooks',
  stage: 'spec', park: 'parked-login', column: 'parked', slot: 0,
  branch: 'feat/rate-limit', model: 'opus', park_note_pending: false,
  park_note: '', feedback_pending: false,
  updated_at: '2026-07-25T11:00:00Z', attached: false, consuming_capacity: true,
}

export const ciParkedCard: TaskCard = {
  issue: 46, target: 'jesdi/widget', title: 'Fix nightly digest',
  stage: 'implement', park: 'awaiting-ci', column: 'awaiting-ci', slot: 1,
  branch: 'fix/nightly-digest', model: 'sonnet', park_note_pending: false,
  park_note: '', feedback_pending: false,
  updated_at: '2026-07-25T10:30:00Z', attached: false, consuming_capacity: false,
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
  ],
  capacity: { active: 2, capacity: 3, slots_used: 2, max_slots: 3 },
}

export const queue: QueueView = {
  targets: [{
    target: 'jesdi/widget',
    as_of: '2026-07-25T11:55:00Z',
    stale: false,
    rows: [
      { number: 51, title: 'Rate-limit webhooks', url: 'https://github.com/jesdi/widget/issues/51',
        status: 'Ready', labels: ['auto'], blocked: false, score: 8.5, boost: 1, in_flight: false },
      { number: 41, title: 'Add CSV export', url: 'https://github.com/jesdi/widget/issues/41',
        status: 'In progress', labels: [], blocked: false, score: 7.0, boost: 0, in_flight: true },
      { number: 60, title: 'Migrate to pydantic v2', url: 'https://github.com/jesdi/widget/issues/60',
        status: 'Backlog', labels: [], blocked: true, score: null, boost: 0, in_flight: false },
    ],
  }],
}

export const staleQueue: QueueView = {
  targets: [{ ...queue.targets[0]!, stale: true, as_of: '2026-07-25T09:12:00Z' }],
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
  pending_reply: '',
  ci_run_id: 0,
  effort: 3,
  labels: ['auto'],
}

export const failures: FailuresView = {
  quarantined: [{
    target: 'jesdi/widget', task_issue: 38, blocker_repo: 'jesdi/widget',
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
    { ts: '2026-07-25T11:30:00Z', event: 'stage-started', target: 'jesdi/widget',
      issue: 41, stage: 'implement', model: 'opus', actor: 'dispatcher', detail: '' },
    { ts: '2026-07-25T10:00:00Z', event: 'parked', target: 'jesdi/widget',
      issue: 42, stage: 'implement', model: 'sonnet', actor: 'dispatcher',
      detail: 'question' },
  ],
}

export const noPendingIntents: PendingIntentsView = { intents: [] }
export const pendingReplyIntent: PendingIntentsView = {
  intents: [{ action: 'reply', issue: 42, actor: 'dev@localhost',
              created_at: '2026-07-25T11:58:00Z' }],
}
