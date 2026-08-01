import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, ApiError } from '../lib/api'
import { queryKeys } from './queryKeys'

/**
 * Shared queue-action mutation consumed by the board (Task 7) and the task
 * view (Task 8). Exposes a busy flag, a per-invocation error string, and
 * three action helpers that each post immediately and invalidate the board
 * cache on success so ghost cards re-rank without a manual refresh.
 *
 * The hook's surface is component-agnostic: drop it into any component that
 * needs boost/next/ready actions without modification.
 */
export function useQueueActions() {
  const queryClient = useQueryClient()
  const [queueError, setQueueError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => {
      setQueueError(null)
      void queryClient.invalidateQueries({ queryKey: queryKeys.board })
    },
    onError: (err) =>
      setQueueError(err instanceof ApiError ? err.detail : String(err)),
  })

  return {
    queueError,
    busy: mutation.isPending,
    boost: (issue: number, amount: number) =>
      mutation.mutate(() => api.queueBoost(issue, amount)),
    next: (issue: number) => mutation.mutate(() => api.queueNext(issue, false)),
    ready: (issue: number) => mutation.mutate(() => api.queueReady(issue)),
  }
}
