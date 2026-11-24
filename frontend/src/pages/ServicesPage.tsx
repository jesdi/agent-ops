import { useState } from 'react'
import { useServices } from '../hooks/useResources'
import type { ServiceJob } from '../lib/api'

const hhmm = (iso: string) => new Date(iso).toISOString().slice(11, 16)

function lastRun(job: ServiceJob): string {
  const run = job.last_run
  if (!run) return 'never ran'
  return `${hhmm(run.at)} · ${run.duration_seconds ?? 0} s`
}

function nextRun(job: ServiceJob, now: string): string {
  if (!job.next_run) return '—'
  const mins = Math.round((Date.parse(job.next_run) - Date.parse(now)) / 60_000)
  return mins <= 0 ? 'now' : `in ${mins} min`
}

function Detail({ job, now }: { job: ServiceJob; now: string }) {
  const rows: [string, string][] = [
    ['State', job.state], ['Schedule', job.schedule],
    ['Last run', lastRun(job)], ['Next run', nextRun(job, now)],
  ]
  return (
    <section aria-label="Job detail" className="space-y-2">
      <h2 className="font-semibold">{job.name}</h2>
      <p className="text-ink-muted">{job.description}</p>
      {job.read_only && <p className="text-sm text-ink-muted">read-only</p>}
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-ink-muted">{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

function Group({ title, jobs, selected, onSelect }: {
  title: string; jobs: ServiceJob[]; selected: string | null; onSelect: (id: string) => void
}) {
  return (
    <section>
      <h2 className="mb-1 text-sm font-semibold text-ink-muted">{title}</h2>
      <ul>
        {jobs.map((j) => (
          <li key={j.id}>
            <button
              type="button"
              aria-pressed={selected === j.id}
              onClick={() => onSelect(j.id)}
              className={`w-full px-2 py-1 text-left ${selected === j.id ? 'font-semibold' : ''}`}
            >
              {j.name}
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function ServicesPage() {
  const { data, isPending, isError, error } = useServices()
  const [selected, setSelected] = useState<string | null>(null)
  if (isPending) return <p className="p-4 text-ink-muted">loading services…</p>
  if (isError) return <p className="p-4 text-failed-fg">services unavailable: {error.message}</p>
  const job = [...data.managed, ...data.read_only].find((j) => j.id === selected)
  return (
    <div className="grid gap-6 p-4 md:grid-cols-[18rem_1fr]">
      <div className="space-y-4">
        <Group title="Managed" jobs={data.managed} selected={selected} onSelect={setSelected} />
        <Group title="Root jobs" jobs={data.read_only} selected={selected} onSelect={setSelected} />
      </div>
      {job ? <Detail job={job} now={data.now} /> : <p className="text-ink-muted">Select a job.</p>}
    </div>
  )
}
