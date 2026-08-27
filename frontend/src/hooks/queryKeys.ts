export const queryKeys = {
  board: ['board'] as const,
  budget: ['budget'] as const,
  failures: ['failures'] as const,
  history: ['history'] as const,
  // Issue numbers are per-target: alpha#73 and beta#73 must not share a
  // cache entry, so target leads the key.
  task: (target: string, issue: number) => ['task', target, issue] as const,
  spec: (target: string, issue: number) => ['task', target, issue, 'spec'] as const,
  taskHistory: (target: string, issue: number) => ['task', target, issue, 'history'] as const,
  allTasks: ['task'] as const,
  pendingIntents: ['pending-intents'] as const,
  description: (target: string, issue: number) => ['task', target, issue, 'description'] as const,
}
