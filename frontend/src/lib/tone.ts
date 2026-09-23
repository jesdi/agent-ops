/** Status tone: the one place a status hue meets a surface. Chips are inline
 *  labels; banners are bordered notices. Every class is a full literal —
 *  Tailwind scans source text, so `bg-${tone}-bg` would never be generated. */
export type Tone = 'running' | 'waiting' | 'failed' | 'parked' | 'neutral'

/** Shared by the status chips below and by chips with a non-status hue
 *  (slot chips), so every chip has one shape. */
export const CHIP_SHAPE = 'rounded px-1.5 text-xs font-medium'

export const chip: Record<Tone, string> = {
  running: `${CHIP_SHAPE} bg-running-bg text-running-fg`,
  waiting: `${CHIP_SHAPE} bg-waiting-bg text-waiting-fg`,
  failed: `${CHIP_SHAPE} bg-failed-bg text-failed-fg`,
  parked: `${CHIP_SHAPE} bg-parked-bg text-parked-fg`,
  neutral: `${CHIP_SHAPE} bg-ink/10 text-ink`,
}

export const banner: Record<Tone, string> = {
  running: 'rounded border border-running-fg/30 bg-running-bg px-3 py-2 text-sm text-running-fg',
  waiting: 'rounded border border-waiting-fg/30 bg-waiting-bg px-3 py-2 text-sm text-waiting-fg',
  failed: 'rounded border border-failed-fg/30 bg-failed-bg px-3 py-2 text-sm text-failed-fg',
  parked: 'rounded border border-parked-fg/30 bg-parked-bg px-3 py-2 text-sm text-parked-fg',
  neutral: 'rounded border border-border bg-surface px-3 py-2 text-sm text-ink',
}
