import type { ArtifactsView } from '../lib/api'
import { useTaskArtifacts } from '../hooks/useResources'

export function ArtifactsPanel({ target, issue }: { target: string; issue: number }) {
  const query = useTaskArtifacts(target, issue)
  return (
    <section aria-label="Artifacts" className="overflow-hidden rounded border border-border bg-surface-raised">
      <header className="p-4">
        <h2 className="text-sm font-semibold text-ink">
          Artifacts {query.data && <span className="ml-2 rounded bg-ink/10 px-2 py-0.5 text-xs">{query.data.items.length}</span>}
        </h2>
        <p className="mt-1 text-xs text-ink-muted">Review material from every session of this task.</p>
      </header>
      {query.isPending && <p className="border-t border-border p-4 text-sm text-ink-muted">Loading artifacts…</p>}
      {query.isError && <p role="alert" className="border-t border-border p-4 text-sm text-failed-fg">Could not load artifacts: {query.error.message}</p>}
      {query.data?.items.length === 0 && <p className="border-t border-border p-4 text-sm text-ink-muted">No artifacts yet. Review material will appear here as sessions create it.</p>}
      {query.data?.items.map((artifact) => <ArtifactRow key={artifact.id} artifact={artifact} />)}
      {query.data && query.data.items.length > 0 && (
        <p className="border-t border-border bg-surface px-4 py-3 text-xs text-ink-muted">
          <ArtifactRetention data={query.data} />
        </p>
      )}
    </section>
  )
}

function ArtifactRow({ artifact }: { artifact: ArtifactsView['items'][number] }) {
  return (
    <div className="flex items-center gap-3 border-t border-border p-4">
      <div className="min-w-0 flex-1">
        <h3 className="break-words text-sm font-medium text-ink">{artifact.name}</h3>
        <p className="mt-1 break-words text-xs text-ink-muted">
          {artifact.status === 'expired' ? 'Box copy removed after 30 days'
            : artifact.status === 'unavailable' ? 'Content unavailable'
            : artifact.github_url ? 'GitHub'
            : artifact.media_type === 'text/markdown' ? 'Not published · local copy'
            : 'Web preview'}
          {' · '}{artifact.stage}
        </p>
      </div>
      {artifact.url ? (
        <a href={artifact.url} target="_blank" rel="noopener noreferrer"
          aria-label={`Open ${artifact.name}`}
          className="inline-flex min-h-11 shrink-0 items-center rounded border border-border px-3 text-sm font-medium hover:bg-surface">
          Open ↗
        </a>
      ) : <span className="text-xs text-ink-muted">{artifact.status === 'expired' ? 'Expired' : 'Unavailable'}</span>}
    </div>
  )
}

function ArtifactRetention({ data }: { data: ArtifactsView }) {
  if (data.expired) return 'Box copies were removed after 30 days. GitHub artifacts remain available.'
  if (data.expires_at) return `Box copies will be removed on ${new Date(data.expires_at).toLocaleDateString()} — 30 days after completion. GitHub artifacts will remain available.`
  return 'Links open the latest version in a new tab.'
}
