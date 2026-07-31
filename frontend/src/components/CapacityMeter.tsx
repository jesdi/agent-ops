import type { CapacityView } from '../lib/api'
import { ACCENT_PIP, capacityAccent } from '../lib/capacity'

export function CapacityMeter({ capacity }: { capacity: CapacityView }) {
  const text = (
    <span className="text-sm text-gray-600">
      {capacity.active}/{capacity.capacity} active · slots{' '}
      {capacity.slots_used}/{capacity.max_slots}
    </span>
  )
  // A misconfigured capacity would render an empty strip, which reads as
  // "nothing running" — the opposite of the truth. Show the numbers instead.
  if (capacity.capacity <= 0) return <div className="flex items-center gap-3">{text}</div>

  const pipClass = ACCENT_PIP[capacityAccent(capacity)]
  const pips = Array.from({ length: capacity.capacity }, (_, i) => i < capacity.active)
  return (
    <div className="flex items-center gap-3">
      <div
        role="progressbar"
        aria-label="capacity units in use"
        aria-valuemin={0}
        aria-valuemax={capacity.capacity}
        // aria-valuenow must stay within [aria-valuemin, aria-valuemax] per ARIA spec;
        // clamp it while aria-valuetext carries the truthful overflow state
        aria-valuenow={Math.min(capacity.active, capacity.capacity)}
        aria-valuetext={`${capacity.active} of ${capacity.capacity} capacity units in use`}
        className="flex items-center gap-1"
      >
        {pips.map((filled, i) => (
          <span
            key={i}
            data-testid={filled ? 'cap-pip-filled' : 'cap-pip-empty'}
            className={`h-3 w-3 rounded-sm ${filled ? pipClass : 'bg-gray-200'}`}
          />
        ))}
      </div>
      {text}
    </div>
  )
}
