import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { QueueTable } from '../components/QueueTable'
import { queryKeys } from '../hooks/queryKeys'
import { useQueue } from '../hooks/useResources'
import { api, ApiError } from '../lib/api'

export function QueuePage() {
  const queueQuery = useQueue()
  const queryClient = useQueryClient()
  const [actionError, setActionError] = useState<string | null>(null)

  const run = useMutation({
    mutationFn: (fn: () => Promise<unknown>) => fn(),
    onSuccess: () => {
      setActionError(null)
      void queryClient.invalidateQueries({ queryKey: queryKeys.queue })
    },
    onError: (err) =>
      setActionError(err instanceof ApiError ? err.detail : String(err)),
  })

  if (queueQuery.isPending) return <p className="p-4 text-gray-500">loading queue…</p>
  if (queueQuery.isError) {
    return <p className="p-4 text-red-600">queue unavailable: {queueQuery.error.message}</p>
  }

  return (
    <div className="flex flex-col gap-4 p-4">
      {queueQuery.data.targets.map((target) => (
        <QueueTable
          key={target.target}
          target={target}
          error={actionError}
          onBoost={(issue, amount) => run.mutate(() => api.queueBoost(issue, amount))}
          onNext={(issue) => run.mutate(() => api.queueNext(issue, false))}
          onReady={(issue) => run.mutate(() => api.queueReady(issue))}
        />
      ))}
    </div>
  )
}
