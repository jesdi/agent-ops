import { useState } from 'react'
import type { ServiceJob } from '../lib/api'
import { useServiceActions } from '../hooks/useServiceActions'

type Kind = 'every' | 'daily' | 'calendar'

/** Schedule editor of a managed job: locked while a change applies; a refusal shows its message. */
export function ScheduleEditor({ job }: { job: ServiceJob }) {
  const { schedule } = useServiceActions()
  const [kind, setKind] = useState<Kind>('every')
  const [every, setEvery] = useState('')
  const [time, setTime] = useState('')
  const [tz, setTz] = useState('')
  const [calendar, setCalendar] = useState('')
  const body = kind === 'every' ? { every: Number(every) }
    : kind === 'daily' ? { daily: time, tz } : { calendar }
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    schedule.mutate({ job: job.id, body })
  }
  return (
    <form onSubmit={submit} aria-label="Schedule editor" className="space-y-1">
      <label>Schedule type{' '}
        <select value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
          <option value="every">Every N minutes</option>
          <option value="daily">Daily at</option>
          <option value="calendar">Custom</option>
        </select>
      </label>
      {kind === 'every' && (
        <label> Minutes <input value={every} onChange={(e) => setEvery(e.target.value)} /></label>
      )}
      {kind === 'daily' && (
        <>
          <label> Time <input value={time} onChange={(e) => setTime(e.target.value)} placeholder="HH:MM" /></label>
          <label> Timezone <input value={tz} onChange={(e) => setTz(e.target.value)} /></label>
        </>
      )}
      {kind === 'calendar' && (
        <label> Expression <input value={calendar} onChange={(e) => setCalendar(e.target.value)} /></label>
      )}
      <button type="submit" disabled={job.change?.state === 'applying' || schedule.isPending}>Save schedule</button>
      {schedule.error && <p className="text-failed-fg">{schedule.error.message}</p>}
    </form>
  )
}
