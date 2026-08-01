export const queryKeys = {
  board: ['board'] as const,
  budget: ['budget'] as const,
  failures: ['failures'] as const,
  history: ['history'] as const,
  task: (issue: number) => ['task', issue] as const,
  spec: (issue: number) => ['task', issue, 'spec'] as const,
  taskHistory: (issue: number) => ['task', issue, 'history'] as const,
  allTasks: ['task'] as const,
  pendingIntents: ['pending-intents'] as const,
}
