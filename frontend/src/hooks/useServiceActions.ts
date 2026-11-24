import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { queryKeys } from './queryKeys'

/** Run now / Stop run for a managed job. Stop never asks for confirmation. */
export function useServiceActions() {
  const queryClient = useQueryClient()
  const onSettled = () => queryClient.invalidateQueries({ queryKey: queryKeys.services })
  const run = useMutation({ mutationFn: api.runService, onSettled })
  const stop = useMutation({ mutationFn: api.stopService, onSettled })
  return { run, stop }
}
