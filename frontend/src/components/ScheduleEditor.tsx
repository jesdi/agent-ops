import { useState } from 'react'
import type { ServiceJob } from '../lib/api'
import { FIELD, PRIMARY } from '../lib/controls'
import { useServiceActions } from '../hooks/useServiceActions'

type Kind = 'every' | 'daily' | 'calendar'

const LABEL = 'flex flex-col gap-0.5 text-xs text-ink-muted'

type Current = ServiceJob['current_schedule']

/** The editor's fields on the job's current schedule; empty when it is not known. */
function startValues(current: Current) {
  const s = { every: null, daily: null, tz: null, calendar: null, ...current }
  return {
    kind: kindOf(s), every: s.every === null ? '' : String(s.every),
    time: s.daily ?? '', tz: s.tz ?? '', calendar: s.calendar ?? '',
  }
}

function kindOf(s: { daily?: string | null; calendar?: string | null }): Kind {
  if (s.daily) return 'daily'
  return s.calendar ? 'calendar' : 'every'
}

/** Schedule editor of a managed job, opened on its current schedule: locked while a change
 * applies, or when `locked` (no services config installed); a refusal shows its message. */
export function ScheduleEditor({ job, locked }: { job: ServiceJob; locked: boolean }) {
  const { schedule } = useServiceActions()
  const start = startValues(job.current_schedule)
  const [kind, setKind] = useState<Kind>(start.kind)
  const [every, setEvery] = useState(start.every)
  const [time, setTime] = useState(start.time)
  const [tz, setTz] = useState(start.tz)
  const [calendar, setCalendar] = useState(start.calendar)
  const body = { every: { every: Number(every) }, daily: { daily: time, tz }, calendar: { calendar } }[kind]
  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    schedule.mutate({ job: job.id, body })
  }
  return (
    <form onSubmit={submit} aria-label="Schedule editor" className="space-y-1.5 border-t border-border pt-3">
      <fieldset disabled={locked} className="flex flex-wrap items-end gap-2">
        <label className={LABEL}>Schedule type
          <select className={`${FIELD} text-ink`} value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            <option value="every">Every N minutes</option>
            <option value="daily">Daily at</option>
            <option value="calendar">Custom</option>
          </select>
        </label>
        {kind === 'every' && (
          <label className={LABEL}>Minutes
            <input className={`${FIELD} w-24 text-ink`} inputMode="numeric" value={every} onChange={(e) => setEvery(e.target.value)} />
          </label>
        )}
        {kind === 'daily' && (
          <>
            <label className={LABEL}>Time
              <input className={`${FIELD} w-24 text-ink`} value={time} onChange={(e) => setTime(e.target.value)} placeholder="HH:MM" />
            </label>
            <label className={LABEL}>Timezone
              <input className={`${FIELD} w-44 text-ink`} value={tz} onChange={(e) => setTz(e.target.value)} placeholder="Europe/Madrid" />
            </label>
          </>
        )}
        {kind === 'calendar' && (
          <label className={`${LABEL} min-w-0 max-w-sm flex-1 basis-48`}>Expression
            <input className={`${FIELD} font-mono text-ink`} value={calendar} onChange={(e) => setCalendar(e.target.value)} placeholder="Mon..Fri *-*-* 09:00" />
          </label>
        )}
        <button type="submit" className={PRIMARY} disabled={job.change?.state === 'applying' || schedule.isPending}>Save schedule</button>
      </fieldset>
      {schedule.error && <p className="text-sm text-failed-fg">{schedule.error.message}</p>}
    </form>
  )
}
