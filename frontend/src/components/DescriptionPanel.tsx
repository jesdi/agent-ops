import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import { useIssueDescription } from '../hooks/useResources'

/** Collapsible GitHub issue body — the "what is this task" answer without a
 *  round-trip to GitHub. Collapsed by default on claimed tasks; the slim
 *  ghost view opens it, since the description IS that view's content. */
export function DescriptionPanel({ issue, defaultOpen = false }: {
  issue: number
  defaultOpen?: boolean
}) {
  const [open, setOpen] = useState(defaultOpen)
  const desc = useIssueDescription(issue, open)
  return (
    <section data-testid="description-panel" className="rounded border border-gray-300 bg-white">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center justify-between px-4 py-2 text-left text-sm font-semibold text-gray-700"
      >
        <span>{open ? '▾' : '▸'} Description</span>
        {desc.data?.url && (
          <a href={desc.data.url} target="_blank" rel="noreferrer"
             onClick={(e) => e.stopPropagation()}
             className="text-xs font-normal text-blue-600 hover:underline">
            open on GitHub ↗
          </a>
        )}
      </button>
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
