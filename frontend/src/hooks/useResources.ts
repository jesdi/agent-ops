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

export function useTaskDetail(target: string, issue: number, repo = '') {
  const refetchInterval = useFallbackInterval()
  return useQuery({
    queryKey: queryKeys.task(target, issue, repo),
    queryFn: () => api.taskDetail(target, issue, repo),
    refetchInterval,
  })
}

/**
 * On-demand pane history. `enabled` gates the fetch so it never fires on the
 * polled task-detail path — a 2000-line tail is ~150KB. staleTime:0 because
 * the live screen has moved on every time the view is reopened.
 */
export function useTaskHistory(target: string, issue: number, enabled: boolean, repo = '') {
  return useQuery({
    queryKey: queryKeys.taskHistory(target, issue, repo),
    queryFn: () => api.taskHistory(target, issue, repo),
    enabled,
    staleTime: 0,
  })
}

/** Lazy: `enabled` gates the fetch so a collapsed panel costs nothing.
 *  retry:false — the backend already degrades gh failures into the payload. */
export function useIssueDescription(target: string, issue: number, enabled: boolean, repo = '') {
  return useQuery({
    queryKey: queryKeys.description(target, issue, repo),
    queryFn: () => api.taskDescription(target, issue, repo),
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
export function useTaskRequest(target: string, issue: number, pollMs?: number, repo = '') {
  const refetchInterval = useFallbackInterval() || pollMs || false
  return useQuery({
    queryKey: queryKeys.request(target, issue, repo),
    queryFn: () => api.taskRequest(target, issue, repo),
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

export function useTaskArtifacts(target: string, issue: number, repo = '') {
  const refetchInterval = useFallbackInterval()
  return useQuery({
    queryKey: queryKeys.artifacts(target, issue, repo),
    queryFn: () => api.taskArtifacts(target, issue, repo),
    retry: false,
    refetchInterval,
  })
}
