import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { QueueTable } from '../components/QueueTable'
import { queryKeys } from '../hooks/queryKeys'
import { useQueue } from '../hooks/useResources'
import { api, ApiError } from '../lib/api'

export function QueuePage() {
  const queueQuery = useQueue()
  const queryClient = useQueryClient()
  const [errors, setErrors] = useState<Record<string, string | null>>({})

  const run = useMutation({
    mutationFn: ({ run: fn }: { target: string; run: () => Promise<unknown> }) => fn(),
    onSuccess: (_data, { target }) => {
      setErrors((prev) => ({ ...prev, [target]: null }))
      void queryClient.invalidateQueries({ queryKey: queryKeys.queue })
    },
    onError: (err, { target }) =>
      setErrors((prev) => ({
        ...prev,
        [target]: err instanceof ApiError ? err.detail : String(err),
      })),
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
          error={errors[target.target] ?? null}
          onBoost={(issue, amount) =>
            run.mutate({ target: target.target, run: () => api.queueBoost(issue, amount) })
          }
          onNext={(issue) =>
            run.mutate({ target: target.target, run: () => api.queueNext(issue, false) })
          }
          onReady={(issue) =>
            run.mutate({ target: target.target, run: () => api.queueReady(issue) })
          }
        />
      ))}
    </div>
  )
}
