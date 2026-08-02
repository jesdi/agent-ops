import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import { queryKeys } from './queryKeys'

/**
 * Shared queue-action mutation consumed by the board (Task 7) and the task
 * view (Task 8). Exposes a busy flag, a per-invocation error string, and
 * three action helpers that each post immediately and on success invalidate
 * both the board cache (so ghost cards re-rank) AND the individual task's
 * query key (so a slim task-view component that reads `queryKeys.task(issue)`
 * refreshes without needing any board-specific wiring).
 *
 * The hook's surface is fully component-agnostic: drop it into any component
 * that needs boost/next/ready actions without modification.
 */
export function useQueueActions() {
  const queryClient = useQueryClient()
  const [queueError, setQueueError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: ({ fn }: { fn: () => Promise<unknown>; issue: number }) => fn(),
    onSuccess: (_data, { issue }) => {
      setQueueError(null)
      void queryClient.invalidateQueries({ queryKey: queryKeys.board })
      void queryClient.invalidateQueries({ queryKey: queryKeys.task(issue) })
    },
    onError: (err) =>
      setQueueError(err instanceof ApiError ? err.detail : String(err)),
  })

  return {
    queueError,
    busy: mutation.isPending,
    boost: (issue: number, amount: number) =>
      mutation.mutate({ fn: () => api.queueBoost(issue, amount), issue }),
    next: (issue: number) =>
      mutation.mutate({ fn: () => api.queueNext(issue, false), issue }),
    ready: (issue: number) =>
      mutation.mutate({ fn: () => api.queueReady(issue), issue }),
  }
}
