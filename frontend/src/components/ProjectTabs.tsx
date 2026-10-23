import { useState } from 'react'
import { useChangeProject, useProjects } from '../hooks/useProjects'
import type { ProjectTab } from '../lib/api'

/** One tab per configured project (never per repository), empty and paused ones too.
 *  `selected` null is the combined board. */
export function ProjectTabs({ selected, onSelect }: {
  selected: string | null; onSelect: (name: string | null) => void
}) {
  const projects = useProjects().data?.projects ?? []
  if (projects.length === 0) return null
  const project = projects.find((p) => p.name === selected)
  const tab = (name: string | null, label: string) => (
    <button
      key={label}
      type="button"
      role="tab"
      aria-selected={name === selected}
      className={`rounded px-3 py-1 text-sm ${name === selected ? 'bg-surface-raised font-semibold' : 'text-ink-muted'}`}
      onClick={() => onSelect(name)}
    >
      {label}
    </button>
  )
  return (
    <div data-testid="project-tabs">
      <div role="tablist" className="flex flex-wrap gap-1">
        {tab(null, 'All projects')}
        {projects.map((p) => tab(p.name, p.name))}
      </div>
      {project && <ProjectPanel key={project.name} project={project} />}
    </div>
  )
}

function ProjectPanel({ project }: { project: ProjectTab }) {
  const { pending } = project
  return (
    <div role="tabpanel" className="mt-2 flex flex-col gap-1 rounded border p-3 text-sm">
      <p>Effective: <span className="font-medium">{project.state}</span></p>
      <ul className="text-ink-muted">
        {project.repositories.map((r) => <li key={r}>{r}</li>)}
      </ul>
      {pending && (
        <p data-testid="project-pending">
          Pending: {pending.requested} ({pending.phase})
          {pending.blocker && <span className="text-failed-fg"> — {pending.blocker}</span>}
          {pending.pr && <> <a className="underline" href={pending.pr.url}>PR #{pending.pr.number}</a></>}
        </p>
      )}
      <ProjectControls project={project} />
    </div>
  )
}

function ProjectControls({ project }: { project: ProjectTab }) {
  const { change, busy, error } = useChangeProject()
  const [confirming, setConfirming] = useState(false)
  const { pending } = project
  const verb = pending ? (pending.requested === 'enabled' ? 'enable' : 'pause')
    : (project.state === 'enabled' ? 'pause' : 'enable')
  return (
    <>
      {error && <p role="alert" className="text-xs text-failed-fg">{error}</p>}
      {confirming ? (
        <div className="flex items-center gap-2">
          <span>Confirm request to {verb} {project.name}?</span>
          <button type="button" className="rounded border px-3 py-1" onClick={() => setConfirming(false)}>Cancel</button>
          <button
            type="button"
            className="rounded border px-3 py-1 disabled:opacity-50"
            disabled={busy}
            onClick={() => { change({ name: project.name, verb }); setConfirming(false) }}
          >
            Confirm {verb}
          </button>
        </div>
      ) : (
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="self-start rounded border px-3 py-1 capitalize disabled:opacity-50"
            disabled={busy || pending !== null}
            onClick={() => setConfirming(true)}
          >
            {verb}
          </button>
          {pending?.phase === 'blocked' && (
            <button type="button" className="rounded border px-3 py-1 disabled:opacity-50"
              disabled={busy} onClick={() => setConfirming(true)}>
              Retry {verb}
            </button>
          )}
        </div>
      )}
    </>
  )
}
