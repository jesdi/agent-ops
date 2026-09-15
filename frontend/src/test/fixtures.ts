import type {
  BoardView, FailuresView, HistoryView, PendingIntentsView,
  TaskCard, TaskDetail, UsageView, WindowView,
} from '../lib/api'

export const parkedCard: TaskCard = {
  issue: 42, target: 'widget', title: 'Fix login redirect',
  stage: 'implement', park: 'question', column: 'parked', slot: 1,
  branch: 'fix/login-redirect', model: 'sonnet', track: 'standard', park_note_pending: true,
  park_note: 'Should I use the staging redirect URL or prod?', feedback_pending: false,
  updated_at: '2026-07-25T10:00:00Z', consuming_capacity: false,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null, score: null,
  undelivered_messages: 0, wake_blocked: false,
}

export const reviewCard: TaskCard = {
  issue: 44, target: 'widget', title: 'Add search feature',
  stage: 'awaiting-spec-review', park: 'awaiting-review', column: 'needs-review',
  slot: -1,
  branch: 'feat/search', model: 'opus', track: 'standard', park_note_pending: false,
  park_note: 'spec ready for review', feedback_pending: false,
  updated_at: '2026-07-25T09:00:00Z', consuming_capacity: false,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null, score: null,
  undelivered_messages: 0, wake_blocked: false,
}

export const inProgressCard: TaskCard = {
  issue: 41, target: 'widget', title: 'Add CSV export',
  stage: 'implement', park: '', column: 'in-progress', slot: 2,
  branch: 'feat/csv-export', model: 'opus', track: 'standard', park_note_pending: false,
  park_note: '', feedback_pending: false,
  updated_at: '2026-07-25T11:30:00Z', consuming_capacity: true,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null, score: null,
  undelivered_messages: 0, wake_blocked: false,
}

// The motivating pair: both sit in the Parked column, only one holds a unit.
export const loginParkedCard: TaskCard = {
  issue: 45, target: 'widget', title: 'Rate-limit webhooks',
  stage: 'spec', park: 'parked-login', column: 'parked', slot: 0,
  branch: 'feat/rate-limit', model: 'opus', track: 'standard', park_note_pending: false,
  park_note: '', feedback_pending: false,
  updated_at: '2026-07-25T11:00:00Z', consuming_capacity: true,
  claimed_at: '2026-07-25T09:00:00Z', cycle_seconds: null, score: null,
  undelivered_messages: 0, wake_blocked: false,
}

export const ciParkedCard: TaskCard = {
  issue: 46, target: 'widget', title: 'Fix nightly digest',
  stage: 'implement', park: 'awaiting-ci', column: 'awaiting-ci', slot: 1,
  branch: 'fix/nightly-digest', model: 'sonnet', track: 'standard', park_note_pending: false,
  park_note: '', feedback_pending: false,
  updated_at: '2026-07-25T10:30:00Z', consuming_capacity: false,
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
  next_claim: { verdict: 'no-candidates', next_pass_eta: '2026-07-25T12:05:00Z', next_issue: 0, next_target: '', minutes_to_reset: 0, blocked_by: '' },
}

const weekAll: WindowView = { kind: 'weekly', scope: null, used: 0.13, allowance: 0.289, headroom: 0.159, minutes_to_reset: 7320, severity: 'ok' }
const ccusageSession: WindowView = { kind: 'session', scope: null, used: 0.62, allowance: 0.8, headroom: 0.18, minutes_to_reset: 45, severity: 'ok' }

// The default model (Opus) draws on the unscoped windows only, so the weekly
// window binds its gate, not Fable's.
export const usage: UsageView = {
  providers: [{
    provider: 'anthropic', source: 'oauth',
    windows: [
      { kind: 'session', scope: null, used: 0.05, allowance: 0.8, headroom: 0.75, minutes_to_reset: 89, severity: 'ok' },
      weekAll,
      { kind: 'weekly', scope: 'Fable', used: 0.23, allowance: 0.289, headroom: 0.059, minutes_to_reset: 7320, severity: 'close' },
    ],
  }],
  gate: {
    model: 'claude-opus-4-8', provider: 'anthropic', admitted: true,
    note: 'anthropic week: 13% used, allowance 29%, headroom 16 pts, resets in 5d 2h',
    minutes_to_reset: 7320, binding: weekAll,
  },
}

export const usageUnavailable: UsageView = {
  providers: [{ provider: 'anthropic', source: 'unavailable', windows: [] }],
  gate: {
    model: 'claude-opus-4-8', provider: 'anthropic', admitted: false,
    note: 'anthropic: usage unavailable', minutes_to_reset: 0, binding: null,
  },
}

export const usageCcusage: UsageView = {
  providers: [{ provider: 'anthropic', source: 'ccusage', windows: [ccusageSession] }],
  gate: {
    model: 'claude-opus-4-8', provider: 'anthropic', admitted: true,
    note: 'anthropic session: 62% used, allowance 80%, headroom 18 pts, resets in 45m',
    minutes_to_reset: 45, binding: ccusageSession,
  },
}

export const taskDetail: TaskDetail = {
  card: parkedCard,
  pane_tail: '? Should I use the staging redirect URL or prod?\n> ',
  session_alive: true,
  worktree: '/home/agent/worktrees/task-42',
  messages: [],
  delivery_contract: 'will deliver when the session resumes',
  track_when: 'Everything.',
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
