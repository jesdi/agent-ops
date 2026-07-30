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

export function useQueue() {
  const refetchInterval = useFallbackInterval()
  return useQuery({ queryKey: queryKeys.queue, queryFn: api.queue, refetchInterval })
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

export function useTaskDetail(issue: number) {
  const refetchInterval = useFallbackInterval()
  return useQuery({
    queryKey: queryKeys.task(issue),
    queryFn: () => api.taskDetail(issue),
    refetchInterval,
  })
}

/** retry:false — a 404 means "no spec recorded", not a transient failure. */
export function useTaskSpec(issue: number) {
  return useQuery({
    queryKey: queryKeys.spec(issue),
    queryFn: () => api.taskSpec(issue),
    retry: false,
  })
}

/**
 * On-demand pane history. `enabled` gates the fetch so it never fires on the
 * polled task-detail path — a 2000-line tail is ~150KB. staleTime:0 because
 * the live screen has moved on every time the view is reopened.
 */
export function useTaskHistory(issue: number, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.taskHistory(issue),
    queryFn: () => api.taskHistory(issue),
    enabled,
    staleTime: 0,
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
