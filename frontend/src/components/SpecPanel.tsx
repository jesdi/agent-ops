import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { useTaskSpec } from '../hooks/useResources'

/** Rendered only at the awaiting-spec-review gate. Approve is two-step for
 *  the same reason kill is on TaskPage: a phone mis-tap must not approve. */
export function SpecPanel({ issue, busy, onApprove }: {
  issue: number
  busy: boolean
  onApprove: () => void
}) {
  const spec = useTaskSpec(issue)
  const [armed, setArmed] = useState(false)

  return (
    <section
      data-testid="spec-panel"
      className="rounded border border-gray-300 bg-white p-4"
    >
      <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-gray-700">
          spec awaiting review
          {spec.data && (
            <span className="ml-2 font-mono text-xs font-normal text-gray-400">
              {spec.data.path}
            </span>
          )}
        </h2>
        {spec.data && (
          <button
            className="rounded bg-green-600 px-3 py-1 text-sm font-medium text-white disabled:opacity-50"
            disabled={busy}
            onClick={() => {
              if (!armed) { setArmed(true); return }
              setArmed(false)
              onApprove()
            }}
          >
            {armed ? 'tap again to approve' : 'approve spec'}
          </button>
        )}
      </header>
      {spec.isPending && <p className="text-sm text-gray-500">loading spec…</p>}
      {spec.isError && (
        <p className="text-sm text-amber-800">
          {spec.error.message} — fallback:{' '}
          <code>mosh agent-vps -- tmux attach -t task-{issue}</code>
        </p>
      )}
      {spec.data && (
        <div className="max-h-96 overflow-auto text-sm">
          <ReactMarkdown>{spec.data.markdown}</ReactMarkdown>
        </div>
      )}
    </section>
  )
}
