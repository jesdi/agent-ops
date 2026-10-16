export const queryKeys = {
  board: ['board'] as const,
  boardSnapshot: ['board', 'snapshot'] as const,
  usage: ['usage'] as const,
  failures: ['failures'] as const,
  history: ['history'] as const,
  // Issue numbers are per-target and per-repository: alpha#73 and beta#73,
  // or two repositories' #73, must not share a cache entry, so target and
  // repository lead the key.
  task: (target: string, issue: number, repo = '') => ['task', target, repo, issue] as const,
  taskHistory: (target: string, issue: number, repo = '') => ['task', target, repo, issue, 'history'] as const,
  allTasks: ['task'] as const,
  pendingIntents: ['pending-intents'] as const,
  modelSettings: ['settings', 'models'] as const,
  description: (target: string, issue: number, repo = '') => ['task', target, repo, issue, 'description'] as const,
  artifacts: (target: string, issue: number, repo = '') => ['task', target, repo, issue, 'artifacts'] as const,
  request: (target: string, issue: number, repo = '') => ['task', target, repo, issue, 'request'] as const,
}
