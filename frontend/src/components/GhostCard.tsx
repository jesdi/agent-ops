import type { GhostCard } from '../lib/api'
import { chip } from '../lib/tone'
import { AdmissionWarning } from './AdmissionWarning'
import { CompactCard } from './CompactCard'

const ACTION = 'rounded border px-2 text-xs text-ink hover:bg-ink/5 disabled:opacity-50'

/** A ranked, not-yet-claimed candidate: a forecast, not work in flight.
 *  Ranking data and actions sit behind expand. Actions reuse /api/queue/*
 *  unchanged. */
export function GhostCardView({ ghost, isNext, busy, onBoost, onNext, onReady }: {
  ghost: GhostCard
  isNext: boolean
  busy: boolean
  onBoost: (issue: number, amount: number) => void
  onNext: (issue: number) => void
  onReady: (issue: number) => void
}) {
  return (
    <CompactCard
      testId={`ghost-${ghost.number}`}
      drag={{ issue: ghost.number, target: ghost.target, title: ghost.title }}
      to={`/task/${ghost.target}/${ghost.number}`}
      variant="ghost"
      badges={isNext && <span className={chip.running}>next</span>}
      detail={<>
        {(ghost.score != null || ghost.boost !== 0) && (
          <div className="flex flex-wrap items-center gap-2">
            {ghost.score != null && <span>score {ghost.score}</span>}
            {ghost.boost !== 0 && <span>boost {ghost.boost}</span>}
          </div>
        )}
        {ghost.admission && (
          <AdmissionWarning target={ghost.target} issue={ghost.number} admission={ghost.admission} />
        )}
        <div className="mt-2 flex gap-1">
          <button type="button" className={ACTION} disabled={busy} onClick={() => onBoost(ghost.number, 1)}>Boost</button>
          <button type="button" className={ACTION} disabled={busy} onClick={() => onBoost(ghost.number, -1)}>Demote</button>
          <button type="button" className={ACTION} disabled={busy} onClick={() => onNext(ghost.number)}>Next</button>
          <button type="button" className={ACTION} disabled={busy} onClick={() => onReady(ghost.number)}>Ready</button>
        </div>
      </>}
    />
  )
}
