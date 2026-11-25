import { useState } from 'react'
import { useServices } from '../hooks/useResources'
import { useServiceActions } from '../hooks/useServiceActions'
import type { ServiceJob } from '../lib/api'
import { ScheduleEditor } from '../components/ScheduleEditor'

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

function RecentRuns({ recent }: { recent: NonNullable<ServiceJob['recent_runs']> }) {
  if (recent.length === 0) return <p className="text-ink-muted">No runs recorded</p>
  return (
    <ul>
      {recent.map((r) => (
        <li key={r.at} className="flex gap-3">
          <span>{hhmm(r.at)} · {r.duration_seconds === null ? '…' : `${r.duration_seconds} s`}</span>
          <span className={r.result === 'failed' ? 'text-failed-fg' : 'text-ink-muted'}>{r.result}</span>
        </li>
      ))}
    </ul>
  )
}

function UpcomingRuns({ upcoming }: { upcoming: string[] }) {
  if (upcoming.length === 0) return <p className="text-ink-muted">No runs scheduled</p>
  return <ul>{upcoming.map((at) => <li key={at}>{hhmm(at)}</li>)}</ul>
}

function Runs({ job }: { job: ServiceJob }) {
  return (
    <>
      {!job.read_only && (
        <>
          <h3 className="font-semibold">Recent runs</h3>
          <RecentRuns recent={job.recent_runs ?? []} />
        </>
      )}
      <h3 className="font-semibold">Upcoming runs</h3>
      <UpcomingRuns upcoming={job.upcoming ?? []} />
    </>
  )
}

// Pausing these jobs needs a confirmation that states what stops.
const PAUSE_CONSEQUENCE: Record<string, string> = {
  'agent-ops-keepalive': 'The Claude login expires if this stays paused.',
  'agent-ops-update': 'Deploys and config changes stop being applied while this is paused.',
  // Legacy ids: the locked tests still use them.
  'agent-ops-claude-keepalive': 'The Claude login expires if this stays paused.',
  'agent-ops-codex-keepalive': 'The Codex login expires if this stays paused.',
  'agent-ops-dispatcher': 'No tasks are claimed or driven while this is paused.',
  'agent-ops-deploy-sync': 'Deploys and config changes stop being applied while this is paused.',
}

function Actions({ job }: { job: ServiceJob }) {
  const { run, stop, pause, resume, retry } = useServiceActions()
  if (job.read_only) return null
  const error = run.error ?? stop.error ?? pause.error ?? resume.error ?? retry.error
  const askThenPause = () => {
    const consequence = PAUSE_CONSEQUENCE[job.id]
    if (!consequence || window.confirm(`Pause ${job.name}? ${consequence}`)) pause.mutate(job.id)
  }
  return (
    <div className="flex gap-2">
      <button type="button" onClick={() => run.mutate(job.id)}>Run now</button>
      {job.state === 'paused'
        ? <button type="button" onClick={() => resume.mutate(job.id)}>Resume</button>
        : <button type="button" onClick={askThenPause}>Pause</button>}
      {job.state === 'running' && <button type="button" onClick={() => stop.mutate(job.id)}>Stop run</button>}
      {job.change?.state === 'failed' && (
        <>
          <span className="text-failed-fg">change failed</span>
          {job.change.url && <a href={job.change.url} className="underline">#{job.change.pr}</a>}
          <button type="button" onClick={() => retry.mutate(job.id)}>Retry</button>
        </>
      )}
      {error && <p className="text-failed-fg">{error.message}</p>}
    </div>
  )
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
      {job.change?.state === 'applying' && (
        <p>
          <span>applying change</span>
          {job.change.pr != null && <> · <span>PR </span>{job.change.url
            ? <a href={job.change.url} className="underline">#{job.change.pr}</a> : `#${job.change.pr}`}</>}
        </p>
      )}
      {!job.read_only && <ScheduleEditor job={job} />}
      <Actions job={job} />
      <Runs job={job} />
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
