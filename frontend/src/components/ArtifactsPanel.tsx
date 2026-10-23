import { useMutation, useQueryClient } from '@tanstack/react-query'
import { api, type ArtifactsView } from '../lib/api'
import { queryKeys } from '../hooks/queryKeys'
import { useTaskArtifacts } from '../hooks/useResources'
import { chip } from '../lib/tone'

export function ArtifactsPanel({ target, repo = '', issue }: { target: string; repo?: string; issue: number }) {
  const query = useTaskArtifacts(target, issue, repo)
  return (
    <section aria-label="Artifacts" className="overflow-hidden rounded border border-border bg-surface-raised">
      <header className="p-4">
        <h2 className="text-sm font-semibold text-ink">
          Artifacts {query.data && <span className={`ml-2 ${chip.neutral}`}>{query.data.items.length}</span>}
        </h2>
        <p className="mt-1 text-xs text-ink-muted">Review material from every session of this task.</p>
      </header>
      {(query.data?.spec_repo_choices?.length ?? 0) > 0 && query.data && (
        <SpecRepoChoice target={target} repo={repo} issue={issue} choices={query.data.spec_repo_choices} />
      )}
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

/** Publication waits for the operator to pick the repository holding the spec/plan. */
function SpecRepoChoice({ target, repo, issue, choices }: { target: string; repo: string; issue: number; choices: string[] }) {
  const queryClient = useQueryClient()
  const choose = useMutation({
    mutationFn: (specRepo: string) => api.chooseSpecRepo(target, issue, specRepo, repo),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.artifacts(target, issue, repo) }),
  })
  return (
    <div role="group" aria-label="Spec repository" className="border-t border-border p-4">
      <p className="text-sm text-ink">Publication waits for the repository that holds the spec and plan.</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {choices.map((choice) => (
          <button key={choice} type="button" disabled={choose.isPending} onClick={() => choose.mutate(choice)}
            className="min-h-11 rounded border border-border px-3 text-sm font-medium hover:bg-surface">
            Use {choice}
          </button>
        ))}
      </div>
      {choose.isError && <p role="alert" className="mt-2 text-sm text-failed-fg">Could not record the choice: {choose.error.message}</p>}
    </div>
  )
}

function ArtifactRow({ artifact }: { artifact: ArtifactsView['items'][number] }) {
  const label = artifact.repo ? `${artifact.name} (${artifact.repo})` : artifact.name
  return (
    <div className="flex items-center gap-3 border-t border-border p-4">
      <div className="min-w-0 flex-1">
        <h3 className="break-words text-sm font-medium text-ink">{label}</h3>
        <p className="mt-1 break-words text-xs text-ink-muted">
          {artifact.status === 'expired' ? 'Box copy removed after 7 days'
            : artifact.status === 'unavailable' ? 'Content unavailable'
            : artifact.github_url ? 'GitHub'
            : artifact.media_type === 'text/markdown' ? 'Not published · local copy'
            : 'Web preview'}
          {' · '}{artifact.stage}
        </p>
      </div>
      {artifact.url ? (
        <a href={artifact.url} target="_blank" rel="noopener noreferrer"
          aria-label={`Open ${label}`}
          className="inline-flex min-h-11 shrink-0 items-center rounded border border-border px-3 text-sm font-medium hover:bg-surface">
          Open ↗
        </a>
      ) : <span className="text-xs text-ink-muted">{artifact.status === 'expired' ? 'Expired' : 'Unavailable'}</span>}
    </div>
  )
}

function ArtifactRetention({ data }: { data: ArtifactsView }) {
  if (data.expired) return 'Box copies were removed after 7 days. GitHub artifacts remain available.'
  if (data.expires_at) return `Box copies will be removed on ${new Date(data.expires_at).toLocaleDateString()} — 7 days after completion. GitHub artifacts will remain available.`
  return 'Links open the latest version in a new tab.'
}
