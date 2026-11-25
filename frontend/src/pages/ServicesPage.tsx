import { useEffect, useRef, useState } from 'react'
import { useServices } from '../hooks/useResources'
import { useServiceActions } from '../hooks/useServiceActions'
import type { ServiceJob } from '../lib/api'
import { ScheduleEditor } from '../components/ScheduleEditor'
import { BUTTON } from '../lib/controls'
import { formatDuration } from '../lib/format'

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** A run time in UTC, labelled so: "HH:MM UTC" on the day of `now`, "Nov 23 HH:MM UTC" on any other. */
function when(iso: string, now: string): string {
  const d = new Date(iso)
  const day = d.toISOString().slice(0, 10) === new Date(now).toISOString().slice(0, 10)
    ? '' : `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} `
  return `${day}${d.toISOString().slice(11, 16)} UTC`
}

function lastRun(job: ServiceJob, now: string): string {
  const run = job.last_run
  if (!run) return 'never ran'
  return `${when(run.at, now)} · ${run.duration_seconds ?? 0} s`
}

function nextRun(job: ServiceJob, now: string): string {
  if (!job.next_run) return '—'
  const mins = Math.ceil((Date.parse(job.next_run) - Date.parse(now)) / 60_000)
  if (mins <= 0) return 'now'
  return mins < 60 ? `in ${mins} min` : `in ${formatDuration(mins * 60)}`
}

function RecentRuns({ recent, now }: { recent: NonNullable<ServiceJob['recent_runs']>; now: string }) {
  if (recent.length === 0) return <p className="text-ink-muted">No runs recorded</p>
  return (
    <ul>
      {recent.map((r) => (
        <li key={r.at} className="flex gap-3">
          <span>{when(r.at, now)} · {r.duration_seconds === null ? '…' : `${r.duration_seconds} s`}</span>
          <span className={r.result === 'failed' ? 'text-failed-fg' : 'text-ink-muted'}>{r.result}</span>
        </li>
      ))}
    </ul>
  )
}

function UpcomingRuns({ upcoming, now }: { upcoming: string[]; now: string }) {
  if (upcoming.length === 0) return <p className="text-ink-muted">No runs scheduled</p>
  return <ul>{upcoming.map((at) => <li key={at}>{when(at, now)}</li>)}</ul>
}

function Runs({ job, now }: { job: ServiceJob; now: string }) {
  return (
    <>
      {!job.read_only && (
        <>
          <h3 className="pt-2 text-sm font-semibold">Recent runs</h3>
          <RecentRuns recent={job.recent_runs ?? []} now={now} />
        </>
      )}
      <h3 className="pt-2 text-sm font-semibold">Upcoming runs</h3>
      <UpcomingRuns upcoming={job.upcoming ?? []} now={now} />
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

function ChangeFailed({ change, onRetry }: { change: ServiceJob['change']; onRetry: () => void }) {
  if (change?.state !== 'failed') return null
  return (
    <p className="flex flex-wrap items-center gap-2 rounded bg-failed-bg px-2 py-1 text-sm text-failed-fg">
      <span>change failed</span>
      {change.url && <a href={change.url} className="underline">#{change.pr}</a>}
      <button type="button" className={`${BUTTON} text-ink`} onClick={onRetry}>Retry</button>
    </p>
  )
}

function ChangeApplying({ change }: { change: ServiceJob['change'] }) {
  if (change?.state !== 'applying') return null
  return (
    <p className="w-fit rounded bg-ink/5 px-2 py-1 text-sm text-ink-muted">
      <span>applying change</span>
      {change.pr != null && <> · <span>PR </span>{change.url
        ? <a href={change.url} className="underline">#{change.pr}</a> : `#${change.pr}`}</>}
    </p>
  )
}

function Actions({ job, locked }: { job: ServiceJob; locked: boolean }) {
  const { run, stop, pause, resume, retry } = useServiceActions()
  if (job.read_only) return null
  const error = [run, stop, pause, resume, retry].find((m) => m.error)?.error
  const askThenPause = () => {
    const consequence = PAUSE_CONSEQUENCE[job.id]
    if (!consequence || window.confirm(`Pause ${job.name}? ${consequence}`)) pause.mutate(job.id)
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <button type="button" className={BUTTON} onClick={() => run.mutate(job.id)}>Run now</button>
        {job.state === 'paused'
          ? <button type="button" className={BUTTON} disabled={locked} onClick={() => resume.mutate(job.id)}>Resume</button>
          : <button type="button" className={BUTTON} disabled={locked} onClick={askThenPause}>Pause</button>}
        {job.running && (
          <button type="button" className={`${BUTTON} text-failed-fg`} onClick={() => stop.mutate(job.id)}>Stop run</button>
        )}
      </div>
      <ChangeFailed change={job.change} onRetry={() => retry.mutate(job.id)} />
      {error && <p className="text-sm text-failed-fg">{error.message}</p>}
    </div>
  )
}

// `locked`: no services config is installed, so Pause, Resume and a schedule cannot be recorded.
function Detail({ job, now, locked }: { job: ServiceJob; now: string; locked: boolean }) {
  const rows: [string, string][] = [
    // A paused job run by hand stays paused: its run shows next to the state.
    ['State', job.state === 'paused' && job.running ? 'paused · running now' : job.state], ['Schedule', job.schedule],
    ['Last run', lastRun(job, now)], ['Next run', nextRun(job, now)],
  ]
  // On a phone the detail sits under the list: bring it into view when a job is picked.
  const ref = useRef<HTMLElement>(null)
  useEffect(() => { ref.current?.scrollIntoView?.({ block: 'nearest' }) }, [])
  return (
    <section ref={ref} aria-label="Job detail"
      className="min-w-0 scroll-mt-4 space-y-3 self-start rounded border border-border bg-surface-raised p-3 sm:p-4">
      <div>
        <h2 className="font-semibold">{job.name}</h2>
        {job.description !== job.name && <p className="text-sm text-ink-muted">{job.description}</p>}
        {job.read_only && <p className="mt-1 w-fit rounded bg-ink/5 px-1.5 text-xs text-ink-muted">read-only</p>}
      </div>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-ink-muted">{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {job.warning && <p className="rounded bg-waiting-bg px-2 py-1 text-sm text-waiting-fg">{job.warning}</p>}
      <ChangeApplying change={job.change} />
      {!job.read_only && <ScheduleEditor job={job} locked={locked} />}
      <Actions job={job} locked={locked} />
      <div className="space-y-1 text-sm"><Runs job={job} now={now} /></div>
    </section>
  )
}

/** Why a row sorts first, as [word, colour]: paused, a run in progress, a change, a drift warning. */
function rowStatus(j: ServiceJob): [string, string][] {
  const parts: [string, string][] = []
  if (j.read_only) return parts
  if (j.state === 'paused') parts.push(['paused', 'text-ink-muted'])
  if (j.running) parts.push(['running', 'text-ink-muted'])
  if (j.change?.state === 'applying') parts.push(['applying', 'text-ink-muted'])
  if (j.change?.state === 'failed') parts.push(['change failed', 'text-failed-fg'])
  if (j.warning) parts.push(['drift', 'text-waiting-fg'])
  return parts
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
              className={`flex w-full items-baseline justify-between gap-2 rounded px-2 py-1.5 text-left text-sm hover:bg-surface-raised ${selected === j.id ? 'bg-surface-raised font-semibold' : ''}`}
            >
              <span className="min-w-0 break-words">{j.name}</span>
              <span className="shrink-0 text-xs font-normal">
                {rowStatus(j).map(([word, tone], i) => (
                  <span key={word}>{i > 0 && <span className="text-ink-muted"> · </span>}<span className={tone}>{word}</span></span>
                ))}
              </span>
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
  const locked = data.config_installed === false
  return (
    <div className="grid gap-6 p-4 md:grid-cols-[18rem_1fr]">
      {locked && (
        <p className="rounded bg-waiting-bg px-2 py-1 text-sm text-waiting-fg md:col-span-2">
          The services config is not installed on the box yet. Pause, Resume and schedule changes wait for it.
        </p>
      )}
      <div className="space-y-4">
        <Group title="Managed" jobs={data.managed} selected={selected} onSelect={setSelected} />
        <Group title="Root jobs" jobs={data.read_only} selected={selected} onSelect={setSelected} />
      </div>
      {job ? <Detail key={job.id} job={job} now={data.now} locked={locked} /> : <p className="text-sm text-ink-muted">Select a job.</p>}
    </div>
  )
}
