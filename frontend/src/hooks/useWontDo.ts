import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { DraggedCard } from '../components/BoardColumn'
import { api, ApiError } from '../lib/api'
import { queryKeys } from './queryKeys'

/** The won't-do confirm flow. A drop on Wont do never fires an intent by
 *  itself: won't-do retires the task for good, so the operator always
 *  confirms first (mirrors the two-step kill on the task view — no
 *  window.confirm, it blocks polling). */
export function useWontDo() {
  const queryClient = useQueryClient()
  const [candidate, setCandidate] = useState<DraggedCard | null>(null)
  const [error, setError] = useState<string | null>(null)
  const dismiss = () => {
    setCandidate(null)
    setError(null)
  }
  const mutation = useMutation({
    mutationFn: (card: DraggedCard) => api.cancel(card.target, card.issue),
    onSuccess: () => {
      dismiss()
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents })
    },
    onError: (err) => setError(err instanceof ApiError ? err.detail : String(err)),
  })
  return {
    candidate,
    error,
    busy: mutation.isPending,
    propose: setCandidate,
    dismiss,
    confirm: (card: DraggedCard) => mutation.mutate(card),
  }
}
