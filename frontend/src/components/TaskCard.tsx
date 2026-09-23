import type { TaskCard } from '../lib/api'
import { slotBorder, slotChip } from '../lib/capacity'
import { formatDuration, relativeTime, stageLabel } from '../lib/format'
import { CHIP_SHAPE, chip } from '../lib/tone'
import { PendingBadge } from './PendingBadge'
import { AdmissionWarning } from './AdmissionWarning'
import { CompactCard } from './CompactCard'

/** Compact by default: identifier, title link, and a signal line only when
 *  something needs action or explains the column. */
export function TaskCardView({ card, pendingActions }: {
  card: TaskCard
  /** Every pending intent on this issue — one badge each, never collapsed. */
  pendingActions?: readonly string[]
}) {
  return (
    <CompactCard
      testId={`card-${card.issue}`}
      drag={{ issue: card.issue, target: card.target, title: card.title }}
      to={`/task/${card.target}/${card.issue}`}
      variant="task"
      accent={card.slot >= 0 ? slotBorder(card.slot) : 'border-l-transparent'}
      badges={<>
        {/* Colour is never the only signal. */}
        {card.consuming_capacity && <span className="sr-only">holding a capacity unit</span>}
        {card.slot >= 0 && <span className="sr-only">holding E2E slot {card.slot}</span>}
        {(pendingActions ?? []).map((action, index) => (
          <PendingBadge key={`${action}-${index}`} action={action} />
        ))}
      </>}
      stamp={relativeTime(card.updated_at)}
      signals={<>
        <TaskCardSignals card={card} />
        {card.admission && (
          <AdmissionWarning target={card.target} issue={card.issue} admission={card.admission} />
        )}
      </>}
      detail={<TaskCardDetail card={card} />}
    />
  )
}

function TaskCardSignals({ card }: { card: TaskCard }) {
  const signals = [
    card.column === 'in-progress' && (
      <span key="stage" className="text-ink-muted">{stageLabel(card.stage)}</span>
    ),
    card.column === 'parked' && card.park !== '' && (
      <span key="park" className={chip.parked}>parked: {card.park}</span>
    ),
    card.undelivered_messages > 0 && (
      <span key="mail" data-testid="mail-badge" className={chip.neutral} title="queued operator messages">
        ✉ {card.undelivered_messages}
      </span>
    ),
    card.wake_blocked && <span key="wake" className={chip.waiting}>waiting for a free slot</span>,
    card.feedback_pending && <span key="feedback" className={chip.waiting}>feedback queued</span>,
    card.park_note_pending && <span key="notify" className={chip.neutral}>notify pending</span>,
  ].filter(Boolean)
  if (signals.length === 0) return null
  return <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs">{signals}</div>
}

function TaskCardDetail({ card }: { card: TaskCard }) {
  const took = card.stage === 'done' ? card.cycle_seconds : null
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-ink-muted">
      <span>{stageLabel(card.stage)}</span>
      <span>{card.model}</span>
      {card.track && <span>track {card.track}</span>}
      {card.score != null && <span className={chip.neutral}>score {card.score}</span>}
      {card.slot >= 0 && (
        <span data-testid="slot-chip" className={`${CHIP_SHAPE} ${slotChip(card.slot)}`}>
          slot {card.slot}
        </span>
      )}
      {took != null && <span>took {formatDuration(took)}</span>}
      {took == null && card.claimed_at !== '' && <span>claimed {relativeTime(card.claimed_at)}</span>}
    </div>
  )
}
