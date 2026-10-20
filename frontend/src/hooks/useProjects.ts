import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError } from '../lib/api'
import { queryKeys } from './queryKeys'

/** Pending changes move on PR checks and deployment, which no live event carries: always poll. */
export function useProjects() {
  return useQuery({ queryKey: queryKeys.projects, queryFn: api.projects, refetchInterval: 3000 })
}

export function useChangeProject() {
  const queryClient = useQueryClient()
  const mutation = useMutation({
    mutationFn: ({ name, verb }: { name: string; verb: 'enable' | 'pause' }) =>
      api.changeProject(name, verb),
    onSettled: () => queryClient.invalidateQueries({ queryKey: queryKeys.projects }),
  })
  const error = mutation.error instanceof ApiError ? mutation.error.detail
    : mutation.error ? String(mutation.error) : null
  return { change: mutation.mutate, busy: mutation.isPending, error }
}
