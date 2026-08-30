import { Link } from 'react-router'
import type { EventEntry } from '../lib/api'
import { relativeTime } from '../lib/format'

const EVENT_COLORS: Record<string, string> = {
  claimed: 'bg-blue-100 text-blue-700',
  'stage-started': 'bg-gray-100 text-gray-700',
  parked: 'bg-purple-100 text-purple-700',
  resumed: 'bg-emerald-100 text-emerald-700',
  'login-code-injected': 'bg-emerald-100 text-emerald-700',
  'pr-opened': 'bg-emerald-100 text-emerald-700',
  failed: 'bg-red-100 text-red-700',
  'intent-applied': 'bg-amber-100 text-amber-700',
}

export function TimelineList({ events }: { events: EventEntry[] }) {
  return (
    <ol className="flex flex-col gap-1">
      {events.map((e, i) => (
        <li
          key={`${e.ts}-${i}`}
          data-testid="timeline-row"
          className="flex flex-wrap items-center gap-2 border-b border-gray-100 py-1.5 text-sm"
        >
          <span className="w-20 shrink-0 text-xs text-gray-400">{relativeTime(e.ts)}</span>
          <span className={`rounded px-1.5 text-xs ${EVENT_COLORS[e.event] ?? 'bg-gray-100'}`}>
            {e.event}
          </span>
          {e.target ? (
            <Link to={`/task/${e.target}/${e.issue}`} className="text-blue-600 hover:underline">
              {e.target}#{e.issue}
            </Link>
          ) : (
            <span className="text-gray-500">#{e.issue}</span>
          )}
          {e.stage && <span className="text-gray-500">{e.stage}</span>}
          {e.model && <span className="text-gray-500">{e.model}</span>}
          <span className="text-xs text-gray-400">by {e.actor}</span>
          {e.detail && <span className="text-xs text-gray-500">{e.detail}</span>}
        </li>
      ))}
    </ol>
  )
}
