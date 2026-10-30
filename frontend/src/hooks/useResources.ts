import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { queryKeys } from './queryKeys'
import { useLiveConnected } from './useLiveUpdates'

function useFallbackInterval(): number | false {
  return useLiveConnected() ? false : 5000
}

export function useTasks() {
  const refetchInterval = useFallbackInterval()
  return useQuery({ queryKey: queryKeys.board, queryFn: api.board, refetchInterval })
}

/** Projects come from targets.yaml, which the console reads once at start:
 *  no live event names them and nothing to poll. */
export function useProjects() {
  return useQuery({ queryKey: queryKeys.projects, queryFn: api.projects })
}

/** Fast first paint; full board data takes over once live checks finish. */
export function useBoardSnapshot(enabled: boolean) {
  const refetchInterval = useFallbackInterval()
  return useQuery({
    queryKey: queryKeys.boardSnapshot,
    queryFn: api.boardSnapshot,
    enabled,
    refetchInterval,
  })
}

export function useUsage() {
  const refetchInterval = useFallbackInterval()
  return useQuery({ queryKey: queryKeys.usage, queryFn: api.usage, refetchInterval })
}

export function useFailures() {
  const refetchInterval = useFallbackInterval()
  return useQuery({ queryKey: queryKeys.failures, queryFn: api.failures, refetchInterval })
}

export function useTimeline() {
  const refetchInterval = useFallbackInterval()
  return useQuery({
    queryKey: queryKeys.history,
    queryFn: () => api.history(200),
    refetchInterval,
  })
}

export function useTaskDetail(repository: string, issue: number) {
  const refetchInterval = useFallbackInterval()
  return useQuery({
    queryKey: queryKeys.task(repository, issue),
    queryFn: () => api.taskDetail(repository, issue),
    refetchInterval,
  })
}

/**
 * On-demand pane history. `enabled` gates the fetch so it never fires on the
 * polled task-detail path — a 2000-line tail is ~150KB. staleTime:0 because
 * the live screen has moved on every time the view is reopened.
 */
export function useTaskHistory(repository: string, issue: number, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.taskHistory(repository, issue),
    queryFn: () => api.taskHistory(repository, issue),
    enabled,
    staleTime: 0,
  })
}

/** Lazy: `enabled` gates the fetch so a collapsed panel costs nothing.
 *  retry:false — the backend already degrades gh failures into the payload. */
export function useIssueDescription(repository: string, issue: number, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.description(repository, issue),
    queryFn: () => api.taskDescription(repository, issue),
    enabled,
    retry: false,
  })
}

/** The unified operator request (plan-approval or answers). retry:false —
 *  null = no request, not a transient failure. Participates in the same
 *  fallback polling as task-detail so an out-of-band clear is eventually
 *  reflected. `pollMs` polls even while live updates are connected (no live
 *  event carries the request). */
// ponytail: fires unconditionally (no enabled guard); add `enabled` if
//   request traffic matters (e.g. many concurrent task panes).
export function useTaskRequest(repository: string, issue: number, pollMs?: number) {
  const refetchInterval = useFallbackInterval() || pollMs || false
  return useQuery({
    queryKey: queryKeys.request(repository, issue),
    queryFn: () => api.taskRequest(repository, issue),
    retry: false,
    refetchInterval,
  })
}

/** Pending intents always poll: they clear only when a dispatcher pass runs. */
export function usePendingIntents() {
  return useQuery({
    queryKey: queryKeys.pendingIntents,
    queryFn: api.pendingIntents,
    refetchInterval: 3000,
  })
}

export function useTaskArtifacts(repository: string, issue: number) {
  const refetchInterval = useFallbackInterval()
  return useQuery({
    queryKey: queryKeys.artifacts(repository, issue),
    queryFn: () => api.taskArtifacts(repository, issue),
    retry: false,
    refetchInterval,
  })
}
