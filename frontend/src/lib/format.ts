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

const STAGE_LABELS: Record<string, string> = {
  spec: 'Writing spec',
  'spec-review': 'Spec review',
  implement: 'Implementing',
  review: 'Review',
  ci: 'Awaiting CI',
  pr: 'PR open',
  'address-review': 'Addressing review',
  done: 'Done',
}

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage
}
