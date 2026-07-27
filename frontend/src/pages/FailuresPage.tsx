import { useMutation, useQueryClient } from '@tanstack/react-query'
import { FailureList } from '../components/FailureList'
import { queryKeys } from '../hooks/queryKeys'
import { useFailures } from '../hooks/useResources'
import { api } from '../lib/api'

export function FailuresPage() {
  const failuresQuery = useFailures()
  const queryClient = useQueryClient()
  const retry = useMutation({
    mutationFn: (issue: number) => api.retry(issue),
    onSuccess: () =>
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents }),
  })

  if (failuresQuery.isPending) return <p className="p-4 text-gray-500">loading failures…</p>
  if (failuresQuery.isError) {
    return <p className="p-4 text-red-600">failures unavailable: {failuresQuery.error.message}</p>
  }
  return (
    <div className="p-4">
      <FailureList failures={failuresQuery.data} onRetry={(i) => retry.mutate(i)} />
    </div>
  )
}
