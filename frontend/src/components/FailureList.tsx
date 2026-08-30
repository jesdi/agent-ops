import type { FailuresView } from '../lib/api'
import { relativeTime } from '../lib/format'

export function FailureList({ failures, onRetry, errors = {}, busy = false }: {
  failures: FailuresView
  onRetry: (target: string, issue: number) => void
  /**
   * Per-task inline retry errors, same convention as GhostCard. Keyed
   * `${target}#${task_issue}` — issue numbers are per-target, so two
   * quarantined tasks sharing a number on different targets must not share
   * an error slot.
   */
  errors?: Readonly<Record<string, string | null>>
  busy?: boolean
}) {
  return (
    <div className="flex flex-col gap-4">
      <section>
        <h2 className="text-sm font-semibold">Quarantined</h2>
        {failures.quarantined.length === 0 && (
          <p className="text-sm text-gray-500">nothing quarantined</p>
        )}
        <ul className="mt-2 flex flex-col gap-2">
          {failures.quarantined.map((q) => {
            const error = errors[`${q.target}#${q.task_issue}`] ?? null
            return (
            <li
              key={`${q.target}#${q.task_issue}`}
              className="flex flex-wrap items-center gap-2 rounded border border-gray-200 bg-white p-3 text-sm"
            >
              <span className="font-mono text-xs">{q.fingerprint}</span>
              <span className="text-gray-500">
                {q.target} task #{q.task_issue} → blocker #{q.blocker_issue} ({q.blocker_repo})
              </span>
              {q.blocker_open === true && (
                <span className="rounded bg-red-100 px-1.5 text-xs text-red-700">blocker open</span>
              )}
              {q.blocker_open === false && (
                <span className="rounded bg-emerald-100 px-1.5 text-xs text-emerald-700">blocker closed</span>
              )}
              {q.blocker_open === null && (
                <span className="rounded bg-gray-100 px-1.5 text-xs text-gray-600">blocker state unknown</span>
              )}
              <span className="text-xs text-gray-400">{relativeTime(q.created_at)}</span>
              <button
                type="button"
                className="ml-auto rounded border px-2 py-0.5 text-xs disabled:opacity-50"
                disabled={busy}
                onClick={() => onRetry(q.target, q.task_issue)}
              >
                Retry
              </button>
              {error && (
                <p
                  data-testid={`retry-error-${q.task_issue}`}
                  className="w-full text-xs text-red-600"
                >
                  {error}
                </p>
              )}
            </li>
            )
          })}
        </ul>
      </section>
      <section>
        <h2 className="text-sm font-semibold">Failure fingerprints</h2>
        <ul className="mt-2 flex flex-col gap-1 text-sm">
          {failures.fingerprints.map((f) => (
            <li key={`${f.fingerprint}-${f.when}`} className="flex gap-2">
              <span className="font-mono text-xs">{f.fingerprint} ({f.repo}#{f.issue})</span>
              <span className="text-xs text-gray-400">{relativeTime(f.when)}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
