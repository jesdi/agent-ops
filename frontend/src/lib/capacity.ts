/** Full-vs-headroom lives here alone, so the header meter and the card
 *  accents can never disagree about what colour "now" is. */
export type Accent = 'amber' | 'blue'

export function capacityAccent(c: { active: number; capacity: number }): Accent {
  return c.active >= c.capacity ? 'amber' : 'blue'
}

export const ACCENT_PIP: Record<Accent, string> = {
  amber: 'bg-amber-500',
  blue: 'bg-blue-500',
}

/** Fixed palette indexed by SLOT NUMBER, so slot 0 is the same hue on the
 *  card border, the card chip and the capacity gauge. Post-fix only live
 *  sessions and login parks hold a slot, so a colour truthfully marks a
 *  running session. Hues are spaced for light-theme contrast and stay
 *  distinguishable under the common red/green deficiencies (blue → amber →
 *  violet → teal → rose → lime). */
export const SLOT_COLORS = [
  'blue-500', 'amber-500', 'violet-500', 'teal-500', 'rose-500', 'lime-600',
] as const

// Full literal class strings, keyed by palette entry — Tailwind scans source
// text, so a class built by interpolation would never reach the stylesheet.
// hover: variants repeat the border colour because hover:border-gray-400 on
// the card would otherwise win on specificity and erase the accent on hover.
const BORDER: Record<string, string> = {
  'blue-500': 'border-l-blue-500 hover:border-l-blue-500',
  'amber-500': 'border-l-amber-500 hover:border-l-amber-500',
  'violet-500': 'border-l-violet-500 hover:border-l-violet-500',
  'teal-500': 'border-l-teal-500 hover:border-l-teal-500',
  'rose-500': 'border-l-rose-500 hover:border-l-rose-500',
  'lime-600': 'border-l-lime-600 hover:border-l-lime-600',
}

const CHIP: Record<string, string> = {
  'blue-500': 'bg-blue-500/10 text-blue-800',
  'amber-500': 'bg-amber-500/10 text-amber-800',
  'violet-500': 'bg-violet-500/10 text-violet-800',
  'teal-500': 'bg-teal-500/10 text-teal-800',
  'rose-500': 'bg-rose-500/10 text-rose-800',
  'lime-600': 'bg-lime-600/10 text-lime-800',
}

const SEGMENT: Record<string, string> = {
  'blue-500': 'bg-blue-500',
  'amber-500': 'bg-amber-500',
  'violet-500': 'bg-violet-500',
  'teal-500': 'bg-teal-500',
  'rose-500': 'bg-rose-500',
  'lime-600': 'bg-lime-600',
}

const hue = (slot: number) => SLOT_COLORS[slot % SLOT_COLORS.length]!

export const slotBorder = (slot: number) => (slot < 0 ? '' : BORDER[hue(slot)])
export const slotChip = (slot: number) => (slot < 0 ? '' : CHIP[hue(slot)])
export const slotSegment = (slot: number) => (slot < 0 ? '' : SEGMENT[hue(slot)])
