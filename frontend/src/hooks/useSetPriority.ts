import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../lib/api'
import { queryKeys } from './queryKeys'

/** The priority-mode write: applied immediately, 200 or 422. The mutation
 *  stays pending until the usage query has refetched, so the caller can show
 *  `pending` as the selection until the server's own answer replaces it. */
export function useSetPriority() {
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: api.setPriority,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.usage }),
  })
  const { error } = mutation
  return {
    setPriority: (mode: string) => mutation.mutate(mode),
    pending: mutation.isPending ? mutation.variables : null,
    error: error && (error instanceof ApiError ? error.detail : String(error)),
  }
}
