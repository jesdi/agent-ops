import { Link } from 'react-router'
import type { TaskCard } from '../lib/api'
import { slotBorder, slotChip } from '../lib/capacity'
import { formatDuration, relativeTime, stageLabel } from '../lib/format'
import { PendingBadge } from './PendingBadge'
import { AdmissionWarning } from './AdmissionWarning'
import { ExpandToggle, useExpand } from './Expand'

const CHIP = 'rounded px-1.5'
const NEUTRAL = `${CHIP} bg-ink/10 text-ink`
const WAITING = `${CHIP} bg-waiting-bg text-waiting-fg`

/** Compact by default: identifier, title link, and a signal line only when
 *  something needs action or explains the column. The article is the drag
 *  source; the title is the only link. */
export function TaskCardView({ card, pendingActions }: {
  card: TaskCard
  /** Every pending intent on this issue — one badge each, never collapsed. */
  pendingActions?: readonly string[]
}) {
  const { expanded, detailId, toggle, onPointerUp } = useExpand()
  const id = `${card.target}#${card.issue}`
  return (
    <article
      data-testid={`card-${card.issue}`}
      draggable
      onDragStart={(event) => {
        event.dataTransfer.setData(
          'application/x-agent-ops-card',
          JSON.stringify({ issue: card.issue, target: card.target, title: card.title }),
        )
      }}
      onPointerUp={onPointerUp}
      className={`rounded border border-l-4 bg-surface-raised px-2.5 py-2 shadow-sm ${
        card.slot >= 0 ? slotBorder(card.slot) : 'border-l-transparent'
      }`}
    >
      {/* Colour is never the only signal. */}
      {card.consuming_capacity && <span className="sr-only">holding a capacity unit</span>}
      {card.slot >= 0 && <span className="sr-only">holding E2E slot {card.slot}</span>}
      {/* Badges wrap among themselves; the stamp and chevron stay top-right. */}
      <div className="flex items-start gap-2 text-xs text-ink-muted">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span>{id}</span>
          {(pendingActions ?? []).map((action, index) => (
            <PendingBadge key={`${action}-${index}`} action={action} />
          ))}
        </div>
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {relativeTime(card.updated_at)}
          <ExpandToggle expanded={expanded} controls={detailId} label={`Details for ${id}`}
            onToggle={toggle} />
        </span>
      </div>
      <Link
        to={`/task/${card.target}/${card.issue}`}
        draggable={false}
        className={`mt-0.5 block text-sm font-medium text-ink hover:underline ${
          expanded ? '' : 'line-clamp-2'
        }`}
      >
        {card.title}
      </Link>
      <TaskCardSignals card={card} />
      {card.admission && (
        <AdmissionWarning target={card.target} issue={card.issue} admission={card.admission} />
      )}
      {expanded && <TaskCardDetail id={detailId} card={card} />}
    </article>
  )
}

function TaskCardSignals({ card }: { card: TaskCard }) {
  const signals = [
    card.column === 'in-progress' && (
      <span key="stage" className="text-ink-muted">{stageLabel(card.stage)}</span>
    ),
    card.column === 'parked' && card.park !== '' && (
      <span key="park" className={`${CHIP} bg-parked-bg text-parked-fg`}>parked: {card.park}</span>
    ),
    card.undelivered_messages > 0 && (
      <span key="mail" data-testid="mail-badge" className={NEUTRAL} title="queued operator messages">
        ✉ {card.undelivered_messages}
      </span>
    ),
    card.wake_blocked && <span key="wake" className={WAITING}>waiting for a free slot</span>,
    card.feedback_pending && <span key="feedback" className={WAITING}>feedback queued</span>,
    card.park_note_pending && <span key="notify" className={NEUTRAL}>notify pending</span>,
  ].filter(Boolean)
  if (signals.length === 0) return null
  return <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs">{signals}</div>
}

function TaskCardDetail({ id, card }: { id: string; card: TaskCard }) {
  const took = card.stage === 'done' ? card.cycle_seconds : null
  return (
    <div id={id} className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 border-t pt-2 text-xs text-ink-muted">
      <span>{card.model}</span>
      {card.track && <span>track {card.track}</span>}
      {card.score != null && <span className={`${CHIP} bg-ink/10`}>score {card.score}</span>}
      {card.slot >= 0 && (
        <span data-testid="slot-chip" className={`${CHIP} ${slotChip(card.slot)}`}>
          slot {card.slot}
        </span>
      )}
      {took != null && <span>took {formatDuration(took)}</span>}
      {took == null && card.claimed_at !== '' && <span>claimed {relativeTime(card.claimed_at)}</span>}
    </div>
  )
}
