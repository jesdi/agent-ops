import { useEffect, useRef, useState, type RefObject } from 'react'
import { useParams } from 'react-router'
import { useTaskRequest } from '../hooks/useResources'
import { api, type OperatorRequest } from '../lib/api'
import { createBridge } from '../lib/reviewBridge'

const NEWER = 'this review page needs a newer console'
const CHANGED = 'the plan changed; your selections were reset to the saved ones'

type Shown = { revision: string; text: string }

/** The review page of the open request, full screen. Only the sandboxed iframe
 *  and, when there is one, a notice: no shell, no panels. */
export function ReviewPage() {
  // The route always carries both params.
  const { target, issue } = useParams() as { target: string; issue: string }
  // Polls even with live updates on: a new revision has no live event.
  const req = useTaskRequest(target, Number(issue), 5000)
  const frameRef = useRef<HTMLIFrameElement>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const shown = useShownPage(req.data, () => setNotice(CHANGED))
  useBridge(frameRef, shown, req.data, target, Number(issue), () => setNotice(NEWER))
  // The changed notice covers the page's top: it goes after a while or on tap.
  // The newer-console notice is an error and stays.
  const dismiss = () => setNotice((n) => (n === CHANGED ? null : n))
  useEffect(() => {
    if (notice !== CHANGED) return
    const timer = setTimeout(() => setNotice(null), 6000)
    return () => clearTimeout(timer)
  }, [notice])

  return (
    <>
      {!shown && <Empty data={req.data} />}
      {shown && (
        <iframe ref={frameRef} data-testid="review-frame" title="review" sandbox="allow-scripts"
          srcDoc={shown.text} className="fixed inset-0 h-full w-full border-0" />
      )}
      {notice && (
        <p data-testid="review-notice" role="status" onClick={dismiss}
          className="fixed inset-x-2 top-2 rounded border border-border bg-surface-raised px-3 py-2 text-center text-sm text-ink shadow">
          {notice}
        </p>
      )}
    </>
  )
}

/** No page to show: say why, once the request route has answered. */
function Empty({ data }: { data: OperatorRequest | null | undefined }) {
  if (data === undefined) return null
  const text = data?.content.kind === 'unavailable' ? data.content.reason : 'no open request'
  return <p className="p-4 text-center text-sm text-ink-muted">{text}</p>
}

/** The page on screen: it changes only when the request's revision changes. */
function useShownPage(data: OperatorRequest | null | undefined, onChanged: () => void): Shown | null {
  const [shown, setShown] = useState<Shown | null>(null)
  if (data?.content.kind === 'readable' && data.revision !== shown?.revision) {
    if (shown) onChanged()
    setShown({ revision: data.revision, text: data.content.text })
  }
  return shown
}

/** One bridge per page on screen, bound to the iframe's window and revision. */
function useBridge(frameRef: RefObject<HTMLIFrameElement | null>, shown: Shown | null,
  data: OperatorRequest | null | undefined, target: string, issue: number, onNewer: () => void) {
  // restore sends the latest saved answers, not the ones of the first load.
  const latest = useRef({ data, onNewer })
  latest.current = { data, onNewer }

  useEffect(() => {
    const frameWindow = frameRef.current?.contentWindow
    if (!shown || !frameWindow) return
    const bridge = createBridge({
      frameWindow,
      revision: shown.revision,
      post: (body) => { api.answers(target, issue, body).catch(() => {}) },
      onNotice: () => latest.current.onNewer(),
      onReady: () => bridge.restore(latest.current.data?.answers ?? {}),
    })
    // A draft the page sent just before pagehide can arrive after it: while
    // hidden, every message flushes at once.
    let hidden = false
    const onMessage = (e: MessageEvent) => { bridge.handleMessage(e); if (hidden) bridge.flush() }
    const onHide = () => { hidden = true; bridge.flush() }
    const onShow = () => { hidden = false }
    window.addEventListener('message', onMessage)
    window.addEventListener('pagehide', onHide)
    window.addEventListener('pageshow', onShow)
    return () => {
      window.removeEventListener('message', onMessage)
      window.removeEventListener('pagehide', onHide)
      window.removeEventListener('pageshow', onShow)
      // Leaving the route (or a new revision) must not lose the pending draft;
      // the server drops a draft whose revision is stale.
      bridge.flush()
    }
  }, [frameRef, shown, target, issue])
}
