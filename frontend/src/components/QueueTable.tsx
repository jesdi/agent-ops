import type { TargetQueue } from '../lib/api'
import { relativeTime } from '../lib/format'

export interface QueueTableProps {
  target: TargetQueue
  onBoost: (issue: number, amount: number) => void
  onNext: (issue: number) => void
  onReady: (issue: number) => void
  error: string | null
}

export function QueueTable({ target, onBoost, onNext, onReady, error }: QueueTableProps) {
  return (
    <div className="rounded border border-gray-200 bg-white">
      <div className="flex items-center justify-between border-b border-gray-100 px-3 py-2">
        <h2 className="text-sm font-semibold">{target.target}</h2>
        {target.stale && (
          <span
            data-testid="stale-banner"
            className="rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-800"
          >
            stale — as of {relativeTime(target.as_of)}
          </span>
        )}
      </div>
      {error && <p className="px-3 py-1 text-xs text-red-600">{error}</p>}
      <table className="w-full text-sm">
        <thead>
          <tr className="text-left text-xs text-gray-500">
            <th className="px-3 py-1">#</th>
            <th className="px-3 py-1">Title</th>
            <th className="px-3 py-1">Status</th>
            <th className="px-3 py-1">Score</th>
            <th className="px-3 py-1">Boost</th>
            <th className="px-3 py-1">Actions</th>
          </tr>
        </thead>
        <tbody>
          {target.rows.map((row) => (
            <tr key={row.number} className="border-t border-gray-100">
              <td className="px-3 py-1.5">
                <a href={row.url} className="text-blue-600 hover:underline">
                  {row.number}
                </a>
              </td>
              <td className="px-3 py-1.5">
                {row.title}{' '}
                {row.blocked && (
                  <span className="rounded bg-red-100 px-1.5 text-xs text-red-700">blocked</span>
                )}{' '}
                {row.in_flight && (
                  <span className="rounded bg-emerald-100 px-1.5 text-xs text-emerald-700">in flight</span>
                )}
              </td>
              <td className="px-3 py-1.5 text-gray-500">{row.status}</td>
              <td className="px-3 py-1.5">{row.score ?? '—'}</td>
              <td className="px-3 py-1.5">{row.boost !== 0 ? row.boost : ''}</td>
              <td className="flex gap-1 px-3 py-1.5">
                <button type="button" className="rounded border px-2 text-xs" onClick={() => onBoost(row.number, 1)}>Boost</button>
                <button type="button" className="rounded border px-2 text-xs" onClick={() => onBoost(row.number, -1)}>Demote</button>
                <button type="button" className="rounded border px-2 text-xs" onClick={() => onNext(row.number)}>Next</button>
                <button type="button" className="rounded border px-2 text-xs" onClick={() => onReady(row.number)}>Ready</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
