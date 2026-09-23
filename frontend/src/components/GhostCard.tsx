import { Link } from 'react-router'
import type { GhostCard } from '../lib/api'
import { AdmissionWarning } from './AdmissionWarning'
import { ExpandToggle, useExpand } from './Expand'

const ACTION = 'rounded border px-2 text-xs text-ink hover:bg-surface disabled:opacity-50'

/** A ranked, not-yet-claimed candidate. Deliberately muted and dashed: it is
 *  a forecast, not work in flight. Compact like a task card: identifier, the
 *  Next badge and the title link; ranking data and actions sit behind expand.
 *  Actions reuse /api/queue/* unchanged. */
export function GhostCardView({ ghost, isNext, busy, onBoost, onNext, onReady }: {
  ghost: GhostCard
  isNext: boolean
  busy: boolean
  onBoost: (issue: number, amount: number) => void
  onNext: (issue: number) => void
  onReady: (issue: number) => void
}) {
  const { expanded, detailId, toggle, onPointerUp } = useExpand()
  const id = `${ghost.target}#${ghost.number}`
  return (
    <div
      data-testid={`ghost-${ghost.number}`}
      draggable
      onDragStart={(e) => {
        e.dataTransfer.setData(
          'application/x-agent-ops-card',
          JSON.stringify({ issue: ghost.number, target: ghost.target, title: ghost.title }),
        )
      }}
      onPointerUp={onPointerUp}
      className="rounded border border-dashed bg-surface px-2.5 py-2 text-ink-muted"
    >
      <div className="flex items-center gap-2 text-xs">
        <span>{id}</span>
        {isNext && (
          <span className="rounded bg-running-bg px-1.5 font-medium text-running-fg">next</span>
        )}
        <span className="ml-auto" />
        <ExpandToggle expanded={expanded} controls={detailId} label={`Details for ${id}`}
          onToggle={toggle} />
      </div>
      <Link
        to={`/task/${ghost.target}/${ghost.number}`}
        draggable={false}
        className={`mt-0.5 block text-sm font-medium hover:underline ${expanded ? '' : 'line-clamp-2'}`}
      >
        {ghost.title}
      </Link>
      {expanded && (
        <div id={detailId} className="mt-2 border-t pt-2 text-xs">
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
        </div>
      )}
    </div>
  )
}
