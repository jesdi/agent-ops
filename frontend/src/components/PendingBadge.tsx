import { chip } from '../lib/tone'

export function PendingBadge({ action }: { action?: string }) {
  return (
    <span
      data-testid="pending-badge"
      className={`inline-flex items-center gap-1 ${chip.waiting}`}
    >
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-waiting-fg" />
      pending{action ? `: ${action}` : ''}
    </span>
  )
}
