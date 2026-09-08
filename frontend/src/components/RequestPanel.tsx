import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { useTaskRequest } from '../hooks/useResources'

/** Unified operator-request panel. Replaces the SpecPanel + ArtifactPanel
 *  dual-mount. Media renderer reused from ArtifactPanel (markdown / sandboxed
 *  HTML / download). Two-step approve shown only for spec-approval + readable. */
export function RequestPanel({ target, issue, busy, onApprove }: {
  target: string
  issue: number
  busy: boolean
  onApprove: () => void
}) {
  const req = useTaskRequest(target, issue)
  const [armed, setArmed] = useState(false)

  const contentText = req.data?.content.kind === 'readable' ? req.data.content.text : null
  // Reset armed when the spec content changes so the operator must re-confirm the revised text.
  // ponytail: fires on first load (contentText: null→null is a no-op); harmless but not free.
  useEffect(() => { setArmed(false) }, [contentText])

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
  const name = content.path.split('/').pop() ?? content.path

  return (
    <section
      data-testid="request-panel"
      className="rounded border border-gray-300 bg-white p-4"
    >
      <header className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-gray-700">
          {kind === 'spec-approval' ? 'spec awaiting review' : 'waiting on your answer'}
          <span className="ml-2 font-mono text-xs font-normal text-gray-400">{content.path}</span>
        </h2>
        {kind === 'spec-approval' && content.kind === 'readable' && (
          <button
            className="rounded bg-green-600 px-3 py-1 text-sm font-medium text-white disabled:opacity-50"
            disabled={busy}
            onClick={() => {
              if (!armed) { setArmed(true); return }
              setArmed(false)
              onApprove()
            }}
          >
            {armed ? 'tap again to approve' : 'approve spec'}
          </button>
        )}
      </header>
      {content.kind === 'unavailable' && (
        <div data-testid="unavailable-recovery" className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          <p className="font-medium">content unavailable</p>
          <p className="mt-1 font-mono text-xs">{content.path}</p>
          <p className="mt-1 text-xs text-amber-700">{content.reason}</p>
          <p className="mt-2 text-xs text-gray-600">
            The spec file could not be read. Use{' '}
            <code>herdr --remote box</code> to inspect the worktree, then reply here once resolved.
          </p>
        </div>
      )}
      {content.kind === 'readable' && (
        <>
          {content.media_type === 'text/markdown' && (
            <div className="max-h-96 overflow-auto text-sm">
              <ReactMarkdown>{content.text}</ReactMarkdown>
            </div>
          )}
          {content.media_type === 'text/html' && (
            <iframe
              title={name}
              sandbox="allow-scripts"
              srcDoc={content.text}
              className="h-96 w-full rounded border border-gray-200"
            />
          )}
          {content.media_type !== 'text/markdown' && content.media_type !== 'text/html' && (
            <a
              download={name}
              href={`data:${content.media_type};charset=utf-8,${encodeURIComponent(content.text)}`}
              className="text-sm text-blue-700 underline"
            >
              download {name}
            </a>
          )}
        </>
      )}
    </section>
  )
}
