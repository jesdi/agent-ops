import { useEffect, useRef, useState, type RefObject } from 'react'
import { useParams } from 'react-router'
import { useTaskRequest } from '../hooks/useResources'
import { api, type OperatorRequest } from '../lib/api'
import { createBridge } from '../lib/reviewBridge'

const NEWER = 'this review page needs a newer console'
const CHANGED = 'the plan changed; your selections were reset to the saved ones'
const UNSAVED = 'could not save your selection; try again'
const SENT = {
  changes: 'sent as changes; the plan session will revise',
  approve: 'approved; implement starts with these answers',
  answers: 'sent; the session continues with your answers',
}
const BUTTON = 'min-h-11 flex-1 rounded-md border px-3 text-sm font-medium'

type Shown = { revision: string; text: string }

/** The review page of the open request, full screen: the sandboxed iframe, the
 *  console's own submit bar and, when there is one, a notice. No shell, no panels. */
export function ReviewPage() {
  // The route always carries both params.
  const { target, issue } = useParams() as { target: string; issue: string }
  // Polls even with live updates on: a new revision has no live event.
  const req = useTaskRequest(target, Number(issue), 5000)
  const frameRef = useRef<HTMLIFrameElement>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const shown = useShownPage(req.data, () => setNotice(CHANGED))
  const bridge = useBridge(frameRef, shown, req.data, target, Number(issue), setNotice)
  // The changed and unsaved notices cover the page's top: they go after a
  // while or on tap (the page keeps its selections). The newer-console notice
  // is an error and stays.
  const dismiss = () => setNotice((n) => (n === NEWER ? n : null))
  useEffect(() => {
    if (!notice || notice === NEWER) return
    const timer = setTimeout(() => setNotice(null), 6000)
    return () => clearTimeout(timer)
  }, [notice])

  return (
    <>
      {!shown && <Empty data={req.data} />}
      {shown && (
        <div className="fixed inset-0 flex flex-col">
          {/* A new window per revision: a late message of the old page then
              fails the bridge's source check instead of posting on the new one. */}
          <iframe key={shown.revision} ref={frameRef} data-testid="review-frame" title="review" sandbox="allow-scripts"
            srcDoc={shown.text} className="min-h-0 w-full flex-1 border-0" />
          <SubmitBar key={`bar-${shown.revision}`} plan={req.data?.kind === 'plan-approval'}
            onSubmit={(submit, sent) => { bridge.current?.submit(submit); setNotice(sent) }} />
        </div>
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

/** The buttons that submit. They are the console's, outside the iframe: the
 *  session writes the page, so a button in it could press itself. */
function SubmitBar({ plan, onSubmit }: { plan: boolean; onSubmit: (submit: 'changes' | 'approve', sent: string) => void }) {
  const [armed, setArmed] = useState(false)
  return (
    <div data-testid="review-bar" className="bg-surface-raised px-4 pb-[calc(0.625rem+env(safe-area-inset-bottom,0px))]">
      <div className="mx-auto flex max-w-[680px] gap-2">
        <button type="button" className={`${BUTTON} border-border bg-surface-raised text-ink`}
          onClick={() => { setArmed(false); onSubmit('changes', plan ? SENT.changes : SENT.answers) }}>
          {plan ? 'Send changes' : 'Send answers'}
        </button>
        {plan && (
          <button type="button" className={`${BUTTON} border-ink bg-ink text-surface-raised`}
            onClick={() => { setArmed(!armed); if (armed) onSubmit('approve', SENT.approve) }}>
            {armed ? 'Tap again to approve' : 'Approve'}
          </button>
        )}
      </div>
    </div>
  )
}

/** No page to show: say why, once the request route has answered. */
function Empty({ data }: { data: OperatorRequest | null | undefined }) {
  if (data === undefined) return null
  const text = data?.content.kind === 'unavailable' ? data.content.reason : 'no open request'
  return <p className="p-4 text-center text-sm text-ink-muted">{text}</p>
}

/** The page on screen: it changes only when the request's revision changes,
 *  and goes when the request has no page any more (closed, or unreadable). */
function useShownPage(data: OperatorRequest | null | undefined, onChanged: () => void): Shown | null {
  const [shown, setShown] = useState<Shown | null>(null)
  if (shown && data !== undefined && data?.content.kind !== 'readable') {
    setShown(null)
  } else if (data?.content.kind === 'readable' && data.revision !== shown?.revision) {
    if (shown) onChanged()
    setShown({ revision: data.revision, text: data.content.text })
  }
  return shown
}

/** One bridge per page on screen, bound to the iframe's window and revision. */
function useBridge(frameRef: RefObject<HTMLIFrameElement | null>, shown: Shown | null,
  data: OperatorRequest | null | undefined, target: string, issue: number,
  onNotice: (notice: string) => void) {
  // restore sends the latest saved answers, not the ones of the first load.
  const latest = useRef({ data, onNotice })
  latest.current = { data, onNotice }
  const current = useRef<ReturnType<typeof createBridge> | null>(null)

  useEffect(() => {
    const frameWindow = frameRef.current?.contentWindow
    if (!shown || !frameWindow) return
    const bridge = createBridge({
      frameWindow,
      revision: shown.revision,
      post: (body) => { api.answers(target, issue, body).catch(() => latest.current.onNotice(UNSAVED)) },
      onNotice: () => latest.current.onNotice(NEWER),
      onReady: () => bridge.restore(latest.current.data?.answers ?? {}),
    })
    current.current = bridge
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
      current.current = null
      window.removeEventListener('message', onMessage)
      window.removeEventListener('pagehide', onHide)
      window.removeEventListener('pageshow', onShow)
      // Leaving the route (or a new revision) must not lose the pending draft;
      // the server drops a draft whose revision is stale.
      bridge.flush()
    }
  }, [frameRef, shown, target, issue])
  return current
}
