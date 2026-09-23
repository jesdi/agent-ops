import { Link } from 'react-router'
import type { EventEntry } from '../lib/api'
import { relativeTime } from '../lib/format'
import { chip, type Tone } from '../lib/tone'

// Events not listed here are neutral.
const EVENT_TONE: Partial<Record<string, Tone>> = {
  claimed: 'running',
  parked: 'parked',
  resumed: 'running',
  failed: 'failed',
}

export function TimelineList({ events }: { events: EventEntry[] }) {
  return (
    <ol className="flex flex-col gap-1">
      {events.map((e, i) => (
        <li
          key={`${e.ts}-${i}`}
          data-testid="timeline-row"
          className="flex flex-wrap items-center gap-2 border-b border-border py-1.5 text-sm"
        >
          <span className="w-20 shrink-0 text-xs text-ink-muted">{relativeTime(e.ts)}</span>
          <span className={chip[EVENT_TONE[e.event] ?? 'neutral']}>
            {e.event}
          </span>
          {e.target ? (
            <Link to={`/task/${e.target}/${e.issue}`} className="underline">
              {e.target}#{e.issue}
            </Link>
          ) : (
            <span className="text-ink-muted">#{e.issue}</span>
          )}
          {e.stage && <span className="text-ink-muted">{e.stage}</span>}
          {e.model && <span className="text-ink-muted">{e.model}</span>}
          <span className="text-xs text-ink-muted">by {e.actor}</span>
          {e.detail && <span className="text-xs text-ink-muted">{e.detail}</span>}
        </li>
      ))}
    </ol>
  )
}
