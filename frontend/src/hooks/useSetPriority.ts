import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, type UsageView } from '../lib/api'
import { queryKeys } from './queryKeys'

/** The priority-mode write: applied immediately, 200 or 422. On success the
 *  stored mode goes straight into the cached usage, so the selection is right
 *  the moment the POST settles; the refetches that follow are not awaited. */
export function useSetPriority() {
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: api.setPriority,
    onSuccess: ({ mode }) => {
      queryClient.setQueryData<UsageView>(queryKeys.usage, (usage) =>
        usage && { ...usage, priority: { ...usage.priority, mode } })
      // The mode reorders every next-launch model, so the board and the task
      // views are stale too, not only the usage panel.
      void queryClient.invalidateQueries({ queryKey: queryKeys.usage })
      void queryClient.invalidateQueries({ queryKey: queryKeys.board })
      void queryClient.invalidateQueries({ queryKey: queryKeys.allTasks })
    },
  })
  const { error } = mutation
  return {
    setPriority: (mode: string) => mutation.mutate(mode),
    pending: mutation.isPending ? mutation.variables : null,
    error: error && (error instanceof ApiError ? error.detail : String(error)),
  }
}
