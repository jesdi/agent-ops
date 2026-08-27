import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { useIssueDescription } from '../hooks/useResources'

/** Collapsible GitHub issue body — the "what is this task" answer without a
 *  round-trip to GitHub. Collapsed by default on claimed tasks; the slim
 *  ghost view opens it, since the description IS that view's content. */
export function DescriptionPanel({ target, issue, defaultOpen = false }: {
  target: string
  issue: number
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const desc = useIssueDescription(target, issue, open)
  return (
    <section data-testid="description-panel" className="rounded border border-gray-300 bg-white">
      <div className="flex items-center justify-between px-4 py-2">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex items-center gap-1 text-left text-sm font-semibold text-gray-700"
        >
          {open ? '▾' : '▸'} Description
        </button>
        {desc.data?.url && (
          <a href={desc.data.url} target="_blank" rel="noreferrer"
             className="text-xs text-blue-600 hover:underline">
            open on GitHub ↗
          </a>
        )}
      </div>
      {open && (
        <div className="border-t border-gray-100 px-4 py-3">
          {desc.isPending && <p className="text-sm text-gray-500">loading description…</p>}
          {desc.isError && (
            <p className="text-sm text-amber-800">description unavailable: {desc.error.message}</p>
          )}
          {desc.data && desc.data.error !== '' && (
            <p className="text-sm text-amber-800">description unavailable: {desc.data.error}</p>
          )}
          {desc.data && desc.data.error === '' && (
            <div className="max-h-96 overflow-auto text-sm">
              <ReactMarkdown>{desc.data.body || '_no description_'}</ReactMarkdown>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
