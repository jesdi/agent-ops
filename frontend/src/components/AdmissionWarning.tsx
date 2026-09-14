import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { queryKeys } from '../hooks/queryKeys'
import type { TaskCard } from '../lib/api'
import { api, ApiError } from '../lib/api'

type Admission = NonNullable<TaskCard['admission']>

function shortModel(model: string): string {
  return model.split('/').at(-1)?.replace(/^claude-/, '') ?? model
}

export function AdmissionWarning({ target, issue, admission }: {
  target: string
  issue: number
  admission: Admission
}) {
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const queryClient = useQueryClient()
  const override = useMutation({
    mutationFn: ({ model, bypassUsage }: { model: string; bypassUsage: boolean }) =>
      api.resume(target, issue, { model, bypassUsage }),
    onSuccess: () => {
      setError(null)
      setOpen(false)
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents })
      void queryClient.invalidateQueries({ queryKey: queryKeys.board })
      void queryClient.invalidateQueries({ queryKey: queryKeys.task(target, issue) })
    },
    onError: (err) => setError(err instanceof ApiError ? err.detail : String(err)),
  })
  const choices = [admission.requested, ...admission.alternatives]

  return (
    <div className="relative mt-2">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="rounded bg-amber-100 px-1.5 py-0.5 text-xs font-medium text-amber-900"
      >
        ⚠ waiting for {shortModel(admission.requested.model)} capacity
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Model capacity options"
          className="mt-2 rounded border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950 shadow-sm"
        >
          <p>{admission.requested.note}</p>
          <p className="mt-2 text-gray-600">
            Keep waiting, run despite the usage limit, or resume with another configured model.
          </p>
          <div className="mt-2 flex flex-col items-start gap-1.5">
            {choices.map((choice) => {
              const bypassUsage = !choice.admitted
              return (
                <button
                  type="button"
                  key={choice.model}
                  disabled={override.isPending}
                  title={choice.note}
                  onClick={() => override.mutate({ model: choice.model, bypassUsage })}
                  className="rounded border border-amber-400 bg-white px-2 py-1 text-left disabled:opacity-50"
                >
                  {bypassUsage ? 'Run anyway with ' : 'Run with '}
                  {shortModel(choice.model)}
                  <span className="ml-1 text-gray-500">
                    ({choice.admitted ? 'capacity available' : 'limited'})
                  </span>
                </button>
              )
            })}
          </div>
          {error && <p className="mt-2 text-red-700">{error}</p>}
        </div>
      )}
    </div>
  )
}
