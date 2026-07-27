export const queryKeys = {
  board: ['board'] as const,
  queue: ['queue'] as const,
  budget: ['budget'] as const,
  failures: ['failures'] as const,
  history: ['history'] as const,
  task: (issue: number) => ['task', issue] as const,
  allTasks: ['task'] as const,
  pendingIntents: ['pending-intents'] as const,
}
