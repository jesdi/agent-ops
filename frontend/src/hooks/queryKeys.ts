export const queryKeys = {
  board: ['board'] as const,
  projects: ['projects'] as const,
  boardSnapshot: ['board', 'snapshot'] as const,
  usage: ['usage'] as const,
  failures: ['failures'] as const,
  history: ['history'] as const,
  // Issue numbers are per-repository: alpha#73 and beta#73 must not share a
  // cache entry, so repository leads the key.
  task: (repository: string, issue: number) => ['task', repository, issue] as const,
  taskHistory: (repository: string, issue: number) => ['task', repository, issue, 'history'] as const,
  allTasks: ['task'] as const,
  pendingIntents: ['pending-intents'] as const,
  modelSettings: ['settings', 'models'] as const,
  description: (repository: string, issue: number) => ['task', repository, issue, 'description'] as const,
  artifacts: (repository: string, issue: number) => ['task', repository, issue, 'artifacts'] as const,
  request: (repository: string, issue: number) => ['task', repository, issue, 'request'] as const,
}
