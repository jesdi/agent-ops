import { Link } from 'react-router'
import type { ArtifactsView } from '../lib/api'
import { useTaskArtifacts, useTaskRequest } from '../hooks/useResources'
import { chip } from '../lib/tone'

export function ArtifactsPanel({ repository, issue }: { repository: string; issue: number }) {
  const query = useTaskArtifacts(repository, issue)
  // #281: the open request's page has its Send bar only on the review route.
  const requestPath = useTaskRequest(repository, issue).data?.content.path
  const route = `/task/${repository}/${issue}/review`
  return (
    <section aria-label="Artifacts" className="overflow-hidden rounded border border-border bg-surface-raised">
      <header className="p-4">
        <h2 className="text-sm font-semibold text-ink">
          Artifacts {query.data && <span className={`ml-2 ${chip.neutral}`}>{query.data.items.length}</span>}
        </h2>
        <p className="mt-1 text-xs text-ink-muted">Review material from every session of this task.</p>
      </header>
      {query.isPending && <p className="border-t border-border p-4 text-sm text-ink-muted">Loading artifacts…</p>}
      {query.isError && <p role="alert" className="border-t border-border p-4 text-sm text-failed-fg">Could not load artifacts: {query.error.message}</p>}
      {query.data && <ArtifactList data={query.data} review={(path) => reviewRoute(path, requestPath, route)} />}
    </section>
  )
}

function ArtifactList({ data, review }: { data: ArtifactsView; review: (path: string) => string | undefined }) {
  if (data.items.length === 0) return <p className="border-t border-border p-4 text-sm text-ink-muted">No artifacts yet. Review material will appear here as sessions create it.</p>
  return (
    <>
      {data.items.map((artifact) => <ArtifactRow key={artifact.id} artifact={artifact} review={review(artifact.path)} />)}
      <p className="border-t border-border bg-surface px-4 py-3 text-xs text-ink-muted">
        <ArtifactRetention data={data} />
      </p>
    </>
  )
}

function reviewRoute(path: string, requestPath: string | undefined, route: string) {
  return requestPath && normalPath(path) === normalPath(requestPath) ? route : undefined
}

const normalPath = (path: string) => path.replace(/\/{2,}/g, '/').replace(/^(\.\/)+/, '')

const openClass = 'inline-flex min-h-11 shrink-0 items-center rounded border border-border px-3 text-sm font-medium hover:bg-surface'

function ArtifactRow({ artifact, review }: { artifact: ArtifactsView['items'][number]; review?: string }) {
  return (
    <div className="flex items-center gap-3 border-t border-border p-4">
      <div className="min-w-0 flex-1">
        <h3 className="break-words text-sm font-medium text-ink">{artifact.name}</h3>
        <p className="mt-1 break-words text-xs text-ink-muted">{artifactSource(artifact)}{' · '}{artifact.stage}</p>
      </div>
      <ArtifactOpen artifact={artifact} review={review} />
    </div>
  )
}

function artifactSource(artifact: ArtifactsView['items'][number]) {
  if (artifact.status === 'expired') return 'Box copy removed after 7 days'
  if (artifact.status === 'unavailable') return 'Content unavailable'
  if (artifact.github_url) return 'GitHub'
  return artifact.media_type === 'text/markdown' ? 'Not published · local copy' : 'Web preview'
}

function ArtifactOpen({ artifact, review }: { artifact: ArtifactsView['items'][number]; review?: string }) {
  if (review) return <Link to={review} aria-label={`Open ${artifact.name}`} className={openClass}>Open</Link>
  if (!artifact.url) return <span className="text-xs text-ink-muted">{artifact.status === 'expired' ? 'Expired' : 'Unavailable'}</span>
  return (
    <a href={artifact.url} target="_blank" rel="noopener noreferrer"
      aria-label={`Open ${artifact.name}`} className={openClass}>
      Open ↗
    </a>
  )
}

function ArtifactRetention({ data }: { data: ArtifactsView }) {
  if (data.expired) return 'Box copies were removed after 7 days. GitHub artifacts remain available.'
  if (data.expires_at) return `Box copies will be removed on ${new Date(data.expires_at).toLocaleDateString()} — 7 days after completion. GitHub artifacts will remain available.`
  return 'Links open the latest version in a new tab.'
}
