import { Link } from 'react-router'
import type { GhostCard } from '../lib/api'

/** A ranked, not-yet-claimed candidate. Deliberately muted and dashed: it is
 *  a forecast, not work in flight. Actions reuse /api/queue/* unchanged. */
export function GhostCardView({ ghost, isNext, busy, onBoost, onNext, onReady }: {
  ghost: GhostCard
  isNext: boolean
  busy: boolean
  onBoost: (issue: number, amount: number) => void
  onNext: (issue: number) => void
  onReady: (issue: number) => void
}) {
  return (
    <div
      data-testid={`ghost-${ghost.number}`}
      className="rounded border border-dashed border-gray-300 bg-gray-50 p-3 text-gray-600"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs text-gray-400">
          {ghost.target}#{ghost.number}
        </span>
        {isNext && (
          <span className="rounded bg-emerald-100 px-1.5 text-xs font-medium text-emerald-700">
            next
          </span>
        )}
        {/* Shown, not hidden: the dispatcher skips it every pass, and a
            silently absent head-of-queue card is what the operator would
            have to debug. See /failures for the blocker. */}
        {ghost.quarantined && (
          <span
            className="rounded bg-amber-100 px-1.5 text-xs font-medium text-amber-800"
            title="held by a quarantine record — the dispatcher skips it until the blocker issue closes"
          >
            quarantined
          </span>
        )}
      </div>
      <Link to={`/task/${ghost.number}`} className="mt-1 block text-sm font-medium hover:underline">
        {ghost.title}
      </Link>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-gray-400">
        {ghost.score != null && <span>score {ghost.score}</span>}
        {ghost.boost !== 0 && <span>boost {ghost.boost}</span>}
      </div>
      <div className="mt-2 flex gap-1">
        <button type="button" className="rounded border px-2 text-xs disabled:opacity-50" disabled={busy} onClick={() => onBoost(ghost.number, 1)}>Boost</button>
        <button type="button" className="rounded border px-2 text-xs disabled:opacity-50" disabled={busy} onClick={() => onBoost(ghost.number, -1)}>Demote</button>
        <button type="button" className="rounded border px-2 text-xs disabled:opacity-50" disabled={busy} onClick={() => onNext(ghost.number)}>Next</button>
        <button type="button" className="rounded border px-2 text-xs disabled:opacity-50" disabled={busy} onClick={() => onReady(ghost.number)}>Ready</button>
      </div>
    </div>
  )
}
