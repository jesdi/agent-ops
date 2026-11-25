import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api } from '../lib/api'
import { queryKeys } from './queryKeys'

/** Run now / Stop run / Pause / Resume / Retry for a managed job. Stop never asks for confirmation. */
export function useServiceActions() {
  const queryClient = useQueryClient()
  const onSettled = () => queryClient.invalidateQueries({ queryKey: queryKeys.services })
  const run = useMutation({ mutationFn: api.runService, onSettled })
  const stop = useMutation({ mutationFn: api.stopService, onSettled })
  const pause = useMutation({ mutationFn: api.pauseService, onSettled })
  const resume = useMutation({ mutationFn: api.resumeService, onSettled })
  const retry = useMutation({ mutationFn: api.retryService, onSettled })
  const schedule = useMutation({ mutationFn: api.setServiceSchedule, onSettled })
  return { schedule, run, stop, pause, resume, retry }
}
