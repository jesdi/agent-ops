export function PendingBadge({ action }: { action?: string }) {
  return (
    <span
      data-testid="pending-badge"
      className="inline-flex items-center gap-1 rounded-full bg-waiting-bg px-2 py-0.5 text-xs font-medium text-waiting-fg"
    >
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-waiting-fg" />
      pending{action ? `: ${action}` : ''}
    </span>
  )
}
