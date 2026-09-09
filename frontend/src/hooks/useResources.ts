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

export function useBudget() {
  const refetchInterval = useFallbackInterval()
  return useQuery({ queryKey: queryKeys.budget, queryFn: api.budget, refetchInterval })
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

export function useTaskDetail(target: string, issue: number) {
  const refetchInterval = useFallbackInterval()
  return useQuery({
    queryKey: queryKeys.task(target, issue),
    queryFn: () => api.taskDetail(target, issue),
    refetchInterval,
  })
}

/**
 * On-demand pane history. `enabled` gates the fetch so it never fires on the
 * polled task-detail path — a 2000-line tail is ~150KB. staleTime:0 because
 * the live screen has moved on every time the view is reopened.
 */
export function useTaskHistory(target: string, issue: number, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.taskHistory(target, issue),
    queryFn: () => api.taskHistory(target, issue),
    enabled,
    staleTime: 0,
  })
}

/** Lazy: `enabled` gates the fetch so a collapsed panel costs nothing.
 *  retry:false — the backend already degrades gh failures into the payload. */
export function useIssueDescription(target: string, issue: number, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.description(target, issue),
    queryFn: () => api.taskDescription(target, issue),
    enabled,
    retry: false,
  })
}

/** The unified operator request (spec-approval or answers). retry:false —
 *  null = no request, not a transient failure. Participates in the same
 *  fallback polling as task-detail so an out-of-band clear is eventually
 *  reflected. */
// ponytail: fires unconditionally (no enabled guard); add `enabled` if
//   request traffic matters (e.g. many concurrent task panes).
export function useTaskRequest(target: string, issue: number) {
  const refetchInterval = useFallbackInterval()
  return useQuery({
    queryKey: queryKeys.request(target, issue),
    queryFn: () => api.taskRequest(target, issue),
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
