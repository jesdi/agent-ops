/** Two most significant units, zero units dropped: 8100 -> "2h 15m". */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const units: [string, number][] = [['d', 86400], ['h', 3600], ['m', 60], ['s', 1]]
  const parts: string[] = []
  let rest = s
  for (const [label, size] of units) {
    const n = Math.floor(rest / size)
    if (n > 0 || (label === 's' && parts.length === 0)) parts.push(`${n}${label}`)
    rest -= n * size
    if (parts.length === 2) break
  }
  return parts.join(' ')
}

export function relativeTime(iso: string, now: Date = new Date()): string {
  const secs = Math.floor((now.getTime() - new Date(iso).getTime()) / 1000)
  if (secs < 10) return 'just now'
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`
  return `${Math.floor(secs / 86400)}d ago`
}

export function formatUtilization(fraction: number): string {
  return `${Math.round(fraction * 100)}%`
}

// Keys MUST be real `Stage` values from dispatcher/state.py — stageLabel is
// called with card.stage, so a key matching no stage renders the raw slug.
// Stages deliberately absent (queued, plan, blocked, failed,
// stalled-on-budget) fall through to their slug, which reads fine as-is.
const STAGE_LABELS: Record<string, string> = {
  spec: 'Writing spec',
  'awaiting-spec-review': 'Spec review',
  implement: 'Implementing',
  'pr-open': 'PR open',
  'address-review': 'Addressing review',
  done: 'Done',
}

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage
}
