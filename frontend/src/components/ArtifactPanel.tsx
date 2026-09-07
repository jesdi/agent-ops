import ReactMarkdown from 'react-markdown'
import { useTaskArtifact } from '../hooks/useResources'

/** The .agent file a parked session is waiting on — questionnaire,
 *  prototype, wizard. Rendered by media type: markdown inline, HTML in a
 *  sandboxed frame, anything else as a download. Nothing awaited (404) →
 *  renders nothing, so it is safe to mount on every parked task. */
export function ArtifactPanel({ target, issue, enabled }: {
  target: string
  issue: number
  enabled: boolean
}) {
  const art = useTaskArtifact(target, issue, enabled)
  if (!art.data) return null
  const { path, media_type, text } = art.data
  const name = path.split('/').pop() ?? path
  return (
    <section
      data-testid="artifact-panel"
      className="rounded border border-purple-300 bg-white p-4"
    >
      <h2 className="mb-2 text-sm font-semibold text-gray-700">
        waiting on your answer
        <span className="ml-2 font-mono text-xs font-normal text-gray-400">{path}</span>
      </h2>
      {media_type === 'text/markdown' && (
        <div className="max-h-96 overflow-auto text-sm">
          <ReactMarkdown>{text}</ReactMarkdown>
        </div>
      )}
      {media_type === 'text/html' && (
        <iframe
          title={name}
          sandbox="allow-scripts"
          srcDoc={text}
          className="h-96 w-full rounded border border-gray-200"
        />
      )}
      {media_type !== 'text/markdown' && media_type !== 'text/html' && (
        <a
          download={name}
          href={`data:${media_type};charset=utf-8,${encodeURIComponent(text)}`}
          className="text-sm text-blue-700 underline"
        >
          download {name}
        </a>
      )}
    </section>
  )
}
