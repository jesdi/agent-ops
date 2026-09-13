import { useTaskArtifacts } from '../hooks/useResources'

export function ArtifactsPanel({ target, issue }: { target: string; issue: number }) {
  const query = useTaskArtifacts(target, issue)
  return (
    <section aria-label="Artifacts" className="overflow-hidden rounded border border-gray-300 bg-white">
      <header className="p-4">
        <h2 className="text-sm font-semibold text-gray-700">
          Artifacts {query.data && <span className="ml-2 rounded bg-gray-100 px-2 py-0.5 text-xs">{query.data.items.length}</span>}
        </h2>
        <p className="mt-1 text-xs text-gray-500">Review material from every session of this task.</p>
      </header>
      {query.isPending && <p className="border-t border-gray-200 p-4 text-sm text-gray-500">Loading artifacts…</p>}
      {query.isError && <p role="alert" className="border-t border-gray-200 p-4 text-sm text-red-700">Could not load artifacts: {query.error.message}</p>}
      {query.data?.items.length === 0 && <p className="border-t border-gray-200 p-4 text-sm text-gray-500">No artifacts yet. Review material will appear here as sessions create it.</p>}
      {query.data?.items.map((artifact) => (
        <div key={artifact.id} className="flex items-center gap-3 border-t border-gray-200 p-4">
          <div className="min-w-0 flex-1">
            <h3 className="break-words text-sm font-medium text-gray-800">{artifact.name}</h3>
            <p className="mt-1 break-words text-xs text-gray-500">
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
              className="inline-flex min-h-11 shrink-0 items-center rounded border border-gray-300 px-3 text-sm font-medium text-blue-700 hover:bg-gray-50">
              Open ↗
            </a>
          ) : <span className="text-xs text-gray-500">{artifact.status === 'expired' ? 'Expired' : 'Unavailable'}</span>}
        </div>
      ))}
      {query.data && query.data.items.length > 0 && (
        <p className="border-t border-gray-200 bg-gray-50 px-4 py-3 text-xs text-gray-500">
          {query.data.expired ? 'Box copies were removed after 30 days. GitHub artifacts remain available.'
            : query.data.expires_at ? `Box copies will be removed on ${new Date(query.data.expires_at).toLocaleDateString()} — 30 days after completion. GitHub artifacts will remain available.`
            : 'Links open the latest version in a new tab.'}
        </p>
      )}
    </section>
  )
}
