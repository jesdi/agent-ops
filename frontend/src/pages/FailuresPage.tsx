import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { FailureList } from '../components/FailureList'
import { queryKeys } from '../hooks/queryKeys'
import { useFailures } from '../hooks/useResources'
import { api, ApiError } from '../lib/api'

export function FailuresPage() {
  const failuresQuery = useFailures()
  const queryClient = useQueryClient()
  // Per-task inline error channel, same convention as GhostCard/useQueueActions:
  // /task/{repository}/{issue}/retry really does 404 ("issue N is not
  // quarantined"), and a swallowed failure is pixel-identical to a landed
  // retry. Keyed `${repository}#${issue}` — see FailureList's errors prop.
  const [errors, setErrors] = useState<Record<string, string | null>>({})

  const retry = useMutation({
    mutationFn: ({ repository, issue }: { repository: string; issue: number }) =>
      api.retry(repository, issue),
    onSuccess: (_data, { repository, issue }) => {
      setErrors((prev) => ({ ...prev, [`${repository}#${issue}`]: null }))
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents })
    },
    onError: (err, { repository, issue }) =>
      setErrors((prev) => ({
        ...prev,
        [`${repository}#${issue}`]: err instanceof ApiError ? err.detail : String(err),
      })),
  })

  if (failuresQuery.isPending) return <p className="p-4 text-ink-muted">loading failures…</p>
  if (failuresQuery.isError) {
    return <p className="p-4 text-failed-fg">failures unavailable: {failuresQuery.error.message}</p>
  }
  return (
    <div className="p-4">
      <FailureList
        failures={failuresQuery.data}
        errors={errors}
        busy={retry.isPending}
        onRetry={(repository, issue) => retry.mutate({ repository, issue })}
      />
    </div>
  )
}
