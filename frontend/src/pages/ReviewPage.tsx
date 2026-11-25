import { useEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { useParams } from 'react-router'
import { useTaskDelivery, useTaskRequest } from '../hooks/useResources'
import { api, type Delivery, type OperatorRequest } from '../lib/api'
import { createBridge, type Answers, type AnswersBody } from '../lib/reviewBridge'

const NEWER = 'this review page needs a newer console'
const CHANGED = 'the plan changed; your selections were reset to the saved ones'
const UNSAVED = 'could not save your selection; try again'
const APPROVED = 'approved; implement starts with these answers'
const PAGE_CHANGED = 'page changed, check and resend'
const BUTTON = 'min-h-11 flex-1 rounded-md border px-3 text-sm font-medium'

type Shown = { revision: string; text: string }
type Submit = NonNullable<AnswersBody['submit']>
/** The tab's last submission, and when its POST was accepted. */
type Sent = { body: AnswersBody; at: number }
type Status = { text: string; resend: Submit | null }

/** The review page of the open request, full screen: the sandboxed iframe, the
 *  console's own submit bar and, when there is one, a notice. No shell, no panels. */
export function ReviewPage() {
  // The route always carries both params.
  const { repository, issue } = useParams() as { repository: string; issue: string }
  // Polls even with live updates on: a new revision has no live event.
  const req = useTaskRequest(repository, Number(issue), 5000)
  const frameRef = useRef<HTMLIFrameElement>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const delivery = useTaskDelivery(repository, Number(issue), 5000)
  const [sent, setSent] = useState<Sent | null>(null)
  const shown = useShownPage(req.data, () => setNotice(CHANGED))
  const state = deliveryOf(sent, delivery.data, delivery.dataUpdatedAt)
  // The tab's submission was dropped because the page changed: the new page
  // gets the dropped answers it still has questions for, not the saved ones.
  const changed = state?.state === 'dropped' && state.reason === 'stale revision'
    && !!shown && shown.revision !== sent?.body.revision
  const dropped = useMemo(() => (changed ? fit(sent!.body.answers, shown!.text) : null), [changed, sent, shown])
  const status: Status | null = state && {
    text: changed ? PAGE_CHANGED : state.state === 'dropped' ? `dropped: ${state.reason}` : state.state,
    resend: state.state === 'dropped' ? state.submit as Submit : null,
  }
  const onSent = (body: AnswersBody) => {
    setSent({ body, at: Date.now() })
    void delivery.refetch()
  }
  const bridge = useBridge(frameRef, shown, dropped ?? req.data?.answers ?? {}, repository, Number(issue), setNotice, onSent)
  useEffect(() => {
    if (!dropped) return
    bridge.current?.restore(dropped)
    setNotice((n) => (n === CHANGED ? null : n))
  }, [dropped, bridge])
  // A drop takes back the approved notice: the approval never reached the session.
  const wasDropped = state?.state === 'dropped'
  useEffect(() => {
    if (wasDropped) setNotice((n) => (n === APPROVED ? null : n))
  }, [wasDropped])
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
      {!shown && <Empty data={req.data} status={status} />}
      {shown && (
        <div className="fixed inset-0 flex flex-col">
          {/* A new window per revision: a late message of the old page then
              fails the bridge's source check instead of posting on the new one. */}
          <iframe key={shown.revision} ref={frameRef} data-testid="review-frame" title="review" sandbox="allow-scripts"
            srcDoc={shown.text} className="min-h-0 w-full flex-1 border-0" />
          <SubmitBar key={`bar-${shown.revision}`} plan={req.data?.kind === 'plan-approval'} status={status}
            onSubmit={(submit) => { bridge.current?.submit(submit); if (submit === 'approve') setNotice(APPROVED) }} />
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
function SubmitBar({ plan, status, onSubmit }: {
  plan: boolean; status: Status | null; onSubmit: (submit: Submit) => void
}) {
  const [armed, setArmed] = useState(false)
  // Resend is the button of the dropped kind pressed again: approve still arms first.
  const press = (submit: Submit) => {
    if (submit === 'changes') { setArmed(false); onSubmit('changes') } else { setArmed(!armed); if (armed) onSubmit('approve') }
  }
  return (
    <div data-testid="review-bar" className="bg-surface-raised px-4 pb-[calc(0.625rem+env(safe-area-inset-bottom,0px))]">
      {status && <DeliveryLine status={status} className="mx-auto max-w-[680px] pt-2 pb-1" />}
      <div className="mx-auto flex max-w-[680px] gap-2">
        <button type="button" className={`${BUTTON} border-border bg-surface-raised text-ink`} onClick={() => press('changes')}>
          {plan ? 'Send changes' : 'Send answers'}
        </button>
        {plan && (
          <button type="button" className={`${BUTTON} border-ink bg-ink text-surface-raised`} onClick={() => press('approve')}>
            {armed ? 'Tap again to approve' : 'Approve'}
          </button>
        )}
        {status?.resend && (
          <button type="button" className={`${BUTTON} border-border bg-surface-raised text-ink`} onClick={() => press(status.resend!)}>
            Resend
          </button>
        )}
      </div>
    </div>
  )
}

function DeliveryLine({ status, className }: { status: Status; className: string }) {
  return <p data-testid="review-delivery" role="status" className={`${className} text-sm text-ink-muted`}>{status.text}</p>
}

/** The delivery of the tab's last submission: none before it sends, and
 *  queued until a read made after its POST, for its revision, says otherwise.
 *  So a delivery of an earlier round, or of another tab, never shows. */
function deliveryOf(sent: Sent | null, data: Delivery | null | undefined, updatedAt: number): Delivery | null {
  if (!sent) return null
  if (!data || updatedAt < sent.at || data.revision !== sent.body.revision) {
    return { state: 'queued', reason: '', submit: sent.body.submit!, revision: sent.body.revision }
  }
  return data
}

/** The answers whose question the page still has (`q` and its `q.note`). */
function fit(answers: Answers, text: string): Answers {
  const ids = new Set([...new DOMParser().parseFromString(text, 'text/html').querySelectorAll('[data-q]')]
    .map((q) => q.getAttribute('data-q')))
  return Object.fromEntries(Object.entries(answers).filter(([k]) => ids.has(k.replace(/\.note$/, ''))))
}

/** No page to show: say why, once the request route has answered. The
 *  tab's submission closes the request once delivered: then say that. */
function Empty({ data, status }: { data: OperatorRequest | null | undefined; status: Status | null }) {
  if (status) return <DeliveryLine status={status} className="p-4 text-center" />
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
  restoring: Answers, repository: string, issue: number,
  onNotice: (notice: string) => void, onSent: (body: AnswersBody) => void) {
  // restore sends the latest answers to restore, not the ones of the first load.
  const latest = useRef({ restoring, onNotice, onSent })
  latest.current = { restoring, onNotice, onSent }
  const current = useRef<ReturnType<typeof createBridge> | null>(null)

  useEffect(() => {
    const frameWindow = frameRef.current?.contentWindow
    if (!shown || !frameWindow) return
    const bridge = createBridge({
      frameWindow,
      revision: shown.revision,
      post: (body) => {
        api.answers(repository, issue, body).then(
          () => { if (body.submit) latest.current.onSent(body) },
          () => latest.current.onNotice(UNSAVED))
      },
      onNotice: () => latest.current.onNotice(NEWER),
      onReady: () => bridge.restore(latest.current.restoring),
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
  }, [frameRef, shown, repository, issue])
  return current
}
