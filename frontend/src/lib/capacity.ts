/** Full-vs-headroom lives here alone, so the header meter and the card
 *  accents can never disagree about what colour "now" is. */
export type Accent = 'amber' | 'blue'

export function capacityAccent(c: { active: number; capacity: number }): Accent {
  return c.active >= c.capacity ? 'amber' : 'blue'
}

// Full literal class strings: Tailwind scans source text, so a class built by
// interpolation would never make it into the stylesheet.
// hover: variants are required because hover:border-gray-400 (shorthand) has
// higher specificity than border-l-blue-500 and would erase the accent on hover.
export const ACCENT_BORDER: Record<Accent, string> = {
  amber: 'border-l-amber-500 hover:border-l-amber-500',
  blue: 'border-l-blue-500 hover:border-l-blue-500',
}

export const ACCENT_PIP: Record<Accent, string> = {
  amber: 'bg-amber-500',
  blue: 'bg-blue-500',
}
