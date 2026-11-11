import type { FailuresView } from '../lib/api'
import { relativeTime } from '../lib/format'
import { chip } from '../lib/tone'

type Quarantined = FailuresView['quarantined'][number]

export function FailureList({ failures, onRetry, errors = {}, busy = new Set() }: {
  failures: FailuresView
  onRetry: (repository: string, issue: number) => void
  /**
   * Per-task inline retry errors, same convention as GhostCard. Keyed
   * `${repository}#${task_issue}` — issue numbers are per-repository, so two
   * quarantined tasks sharing a number on different repositories must not share
   * an error slot.
   */
  errors?: Readonly<Record<string, string | null>>
  /** Keys (`${repository}#${task_issue}`) with a retry in flight. */
  busy?: ReadonlySet<string>
}) {
  return (
    <div className="flex flex-col gap-4">
      <section>
        <h2 className="text-sm font-semibold">Quarantined</h2>
        {failures.quarantined.length === 0 && (
          <p className="text-sm text-ink-muted">nothing quarantined</p>
        )}
        <ul className="mt-2 flex flex-col gap-2">
          {failures.quarantined.map((q) => {
            const key = `${q.repository}#${q.task_issue}`
            return (
              <QuarantineRow
                key={key}
                q={q}
                error={errors[key] ?? null}
                busy={busy.has(key)}
                onRetry={onRetry}
              />
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
              <span className="text-xs text-ink-muted">{relativeTime(f.when)}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}

function QuarantineRow({ q, error, busy, onRetry }: {
  q: Quarantined
  error: string | null
  busy: boolean
  onRetry: (repository: string, issue: number) => void
}) {
  return (
    <li
      role="group"
      aria-label={`${q.repository}#${q.task_issue}`}
      className="flex flex-wrap items-center gap-2 rounded border border-border bg-surface-raised p-3 text-sm"
    >
      {q.readable ? (
        <QuarantineDetails q={q} />
      ) : (
        <>
          <span className="font-mono text-xs">{q.repository}#{q.task_issue}</span>
          <span className={chip.failed}>quarantine record unreadable — still blocking</span>
        </>
      )}
      <button
        type="button"
        className="ml-auto rounded border px-2 py-0.5 text-xs disabled:opacity-50"
        disabled={busy}
        onClick={() => onRetry(q.repository, q.task_issue)}
      >
        Retry
      </button>
      {error && (
        <p
          data-testid={`retry-error-${q.task_issue}`}
          className="w-full text-xs text-failed-fg"
        >
          {error}
        </p>
      )}
    </li>
  )
}

function QuarantineDetails({ q }: { q: Quarantined }) {
  return (
    <>
      <span className="font-mono text-xs">{q.fingerprint}</span>
      <span className="text-ink-muted">
        {q.repository} task #{q.task_issue} → blocker #{q.blocker_issue} ({q.blocker_repo})
      </span>
      {q.blocker_open === true && (
        <span className={chip.failed}>blocker open</span>
      )}
      {q.blocker_open === false && (
        <span className={chip.neutral}>blocker closed</span>
      )}
      {q.blocker_open === null && (
        <span className={chip.neutral}>blocker state unknown</span>
      )}
      <span className="text-xs text-ink-muted">{relativeTime(q.created_at)}</span>
    </>
  )
}
