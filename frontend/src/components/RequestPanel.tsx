import { useState } from 'react'
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
        <p className="text-sm text-gray-500">content unavailable ({content.reason})</p>
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
