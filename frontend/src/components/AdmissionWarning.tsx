import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { queryKeys } from '../hooks/queryKeys'
import type { TaskCard } from '../lib/api'
import { api, ApiError } from '../lib/api'
import { banner, chip } from '../lib/tone'

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
      api.forceRun(target, issue, { model, bypassUsage }),
    onSuccess: () => {
      setError(null)
      setOpen(false)
      void queryClient.invalidateQueries({ queryKey: queryKeys.pendingIntents })
      void queryClient.invalidateQueries({ queryKey: queryKeys.board })
      void queryClient.invalidateQueries({ queryKey: queryKeys.task(target, issue) })
    },
    onError: (err) => setError(err instanceof ApiError ? err.detail : String(err)),
  })
  // Once the stage has a pick its provider is fixed (the server 422s any
  // other); only a stage with no pick yet may switch provider.
  const alternatives = admission.any_provider
    ? admission.alternatives
    : admission.alternatives.filter((a) => a.provider === admission.requested.provider)
  const choices = [admission.requested, ...alternatives]

  return (
    <div className="relative mt-2">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className={chip.waiting}
      >
        ⚠ {shortModel(admission.requested.model)} capacity limited
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Model capacity options"
          className={`mt-2 shadow-sm ${banner.waiting}`}
        >
          {/* Banner shape, card-sized text. */}
          <div className="text-xs">
            <p>{admission.requested.note}</p>
            <p className="mt-2 text-ink-muted">
              Keep waiting, run despite the usage limit, or process this task with another configured model.
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
                    className="rounded border border-waiting-fg/30 bg-surface-raised px-2 py-1 text-left disabled:opacity-50"
                  >
                    {bypassUsage ? 'Run anyway with ' : 'Run with '}
                    {shortModel(choice.model)}
                    <span className="ml-1 text-ink-muted">
                      ({choice.admitted ? 'capacity available' : 'limited'})
                    </span>
                  </button>
                )
              })}
            </div>
            {error && <p className="mt-2 text-failed-fg">{error}</p>}
          </div>
        </div>
      )}
    </div>
  )
}
