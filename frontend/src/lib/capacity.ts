/** Full-vs-headroom lives here alone, so the header meter and the card
 *  accents can never disagree about what colour "now" is. */
export type Accent = 'full' | 'headroom'

export function capacityAccent(c: { active: number; capacity: number }): Accent {
  return c.active >= c.capacity ? 'full' : 'headroom'
}

export const ACCENT_PIP: Record<Accent, string> = {
  full: 'bg-waiting-fg',
  headroom: 'bg-running-fg',
}

/** Slot hues are tokens (--color-slot-N in index.css), indexed by SLOT
 *  NUMBER, so slot 0 is the same hue on the card border, the card chip and
 *  the capacity gauge, in either theme. Post-fix only live sessions and login
 *  parks hold a slot, so a colour truthfully marks a running session. */
export const SLOT_COLORS = ['slot-0', 'slot-1', 'slot-2', 'slot-3', 'slot-4', 'slot-5'] as const

// Full literal class strings, keyed by palette entry — Tailwind scans source
// text, so a class built by interpolation would never reach the stylesheet.
const BORDER: Record<string, string> = {
  'slot-0': 'border-l-slot-0',
  'slot-1': 'border-l-slot-1',
  'slot-2': 'border-l-slot-2',
  'slot-3': 'border-l-slot-3',
  'slot-4': 'border-l-slot-4',
  'slot-5': 'border-l-slot-5',
}

const CHIP: Record<string, string> = {
  'slot-0': 'bg-slot-0/15 text-slot-0-fg',
  'slot-1': 'bg-slot-1/15 text-slot-1-fg',
  'slot-2': 'bg-slot-2/15 text-slot-2-fg',
  'slot-3': 'bg-slot-3/15 text-slot-3-fg',
  'slot-4': 'bg-slot-4/15 text-slot-4-fg',
  'slot-5': 'bg-slot-5/15 text-slot-5-fg',
}

const SEGMENT: Record<string, string> = {
  'slot-0': 'bg-slot-0',
  'slot-1': 'bg-slot-1',
  'slot-2': 'bg-slot-2',
  'slot-3': 'bg-slot-3',
  'slot-4': 'bg-slot-4',
  'slot-5': 'bg-slot-5',
}

const hue = (slot: number) => SLOT_COLORS[slot % SLOT_COLORS.length]!

export const slotBorder = (slot: number) => (slot < 0 ? '' : BORDER[hue(slot)])
export const slotChip = (slot: number) => (slot < 0 ? '' : CHIP[hue(slot)])
export const slotSegment = (slot: number) => (slot < 0 ? '' : SEGMENT[hue(slot)])
