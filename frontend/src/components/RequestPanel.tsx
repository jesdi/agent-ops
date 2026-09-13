import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import type { OperatorRequest } from '../lib/api'
import { useTaskArtifacts, useTaskRequest } from '../hooks/useResources'

/** Review requests offer approval only when their spec content is readable. */
export function RequestPanel({ target, issue, busy, onApprove }: {
  target: string
  issue: number
  busy: boolean
  onApprove: () => void
}) {
  const req = useTaskRequest(target, issue)
  const artifacts = useTaskArtifacts(target, issue)
  const spec = artifacts.data?.items.find((item) => item.id === 'spec')

  if (req.isError) {
    return (
      <section data-testid="request-error" className="rounded border border-red-300 bg-red-50 p-4">
        <p className="text-sm text-red-700">
          failed to load request: {req.error instanceof Error ? req.error.message : 'unknown error'}
        </p>
      </section>
    )
  }
  if (!req.data) return null
  const { kind, content } = req.data

  return (
    <section
      data-testid="request-panel"
      className="rounded border border-gray-300 bg-white p-4"
    >
      <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="min-w-0 break-words text-sm font-semibold text-gray-700">
          {kind === 'spec-approval' ? 'spec awaiting review' : 'waiting on your answer'}
          <span className="ml-2 break-all font-mono text-xs font-normal text-gray-400">{content.path}</span>
        </h2>
        {kind === 'spec-approval' && content.kind === 'readable' && (
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
            {spec?.github_url && <a href={spec.url} target="_blank" rel="noopener noreferrer"
              className="inline-flex min-h-11 items-center justify-center rounded border border-gray-300 px-3 text-sm font-medium text-blue-700">
              View spec on GitHub ↗
            </a>}
            <SpecApproval contentText={content.text} busy={busy} onApprove={onApprove} />
          </div>
        )}
      </header>
      {kind === 'spec-approval' && artifacts.data && !spec?.github_url && (
        <p className="mb-3 rounded border border-amber-300 bg-amber-50 p-3 text-sm text-amber-800">
          Spec hasn’t been published to GitHub yet. {content.kind === 'readable' && 'You can still read and approve the local spec below.'}
        </p>
      )}
      {kind === 'spec-approval' && artifacts.isError && (
        <p className="mb-3 text-sm text-amber-800">Could not load the GitHub spec link. Local review is still available.</p>
      )}
      <RequestContent content={content} />
    </section>
  )
}

function RequestContent({ content }: { content: OperatorRequest['content'] }) {
  if (content.kind === 'unavailable') {
    return (
      <div data-testid="unavailable-recovery" className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
        <p className="font-medium">content unavailable</p>
        <p className="mt-1 font-mono text-xs">{content.path}</p>
        <p className="mt-1 text-xs text-amber-700">{content.reason}</p>
        <p className="mt-2 text-xs text-gray-600">
          The spec file could not be read. Use{' '}
          <code>herdr --remote box</code> to inspect the worktree, then reply here once resolved.
        </p>
      </div>
    )
  }
  const name = content.path.split('/').pop() ?? content.path
  switch (content.media_type) {
    case 'text/markdown':
      return <div className="max-h-96 overflow-auto text-sm"><ReactMarkdown>{content.text}</ReactMarkdown></div>
    case 'text/html':
      return <iframe title={name} sandbox="allow-scripts" srcDoc={content.text}
        className="h-96 w-full rounded border border-gray-200" />
    default:
      return (
        <a download={name} href={`data:${content.media_type};charset=utf-8,${encodeURIComponent(content.text)}`}
          className="text-sm text-blue-700 underline">
          download {name}
        </a>
      )
  }
}

function SpecApproval({ contentText, busy, onApprove }: {
  contentText: string
  busy: boolean
  onApprove: () => void
}) {
  const [armed, setArmed] = useState(false)
  // Every revision requires a fresh confirmation of the reviewed content.
  useEffect(() => { setArmed(false) }, [contentText])
  return (
    <button
      className="min-h-11 rounded bg-green-600 px-3 py-1 text-sm font-medium text-white disabled:opacity-50"
      disabled={busy}
      onClick={() => {
        if (!armed) { setArmed(true); return }
        setArmed(false)
        onApprove()
      }}
    >
      {armed ? 'tap again to approve' : 'approve spec'}
    </button>
  )
}
