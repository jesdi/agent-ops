import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { FailureList } from '../components/FailureList'
import { queryKeys } from '../hooks/queryKeys'
import { useFailures } from '../hooks/useResources'
import { api, ApiError } from '../lib/api'

export function FailuresPage() {
  const failuresQuery = useFailures()
  const queryClient = useQueryClient()
  // Per-task inline error channel, same convention as QueuePage/QueueTable:
  // /task/{issue}/retry really does 404 ("issue N is not quarantined"), and a
  // swallowed failure is pixel-identical to a landed retry.
  const [errors, setErrors] = useState<Record<number, string | null>>({})

  const retry = useMutation({
    mutationFn: (issue: number) => api.retry(issue),
    onSuccess: (_data, issue) => {
      setErrors((prev) => ({ ...prev, [issue]: null }))
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents })
    },
    onError: (err, issue) =>
      setErrors((prev) => ({
        ...prev,
        [issue]: err instanceof ApiError ? err.detail : String(err),
      })),
  })

  if (failuresQuery.isPending) return <p className="p-4 text-gray-500">loading failures…</p>
  if (failuresQuery.isError) {
    return <p className="p-4 text-red-600">failures unavailable: {failuresQuery.error.message}</p>
  }
  return (
    <div className="p-4">
      <FailureList
        failures={failuresQuery.data}
        errors={errors}
        busy={retry.isPending}
        onRetry={(i) => retry.mutate(i)}
      />
    </div>
  )
}
