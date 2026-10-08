import type { components } from './api-types'

export type AnswersBody = components['schemas']['AnswersReq']
export type Answers = AnswersBody['answers']

/** The console side of the review page protocol (v 1). It trusts a message only
 *  when it comes from the iframe's window, and takes only drafts from it: the
 *  session writes the page, so a `submit` in a message is ignored. `submit` is
 *  for the console's own buttons; it posts the page's last set at once. */
export function createBridge({ frameWindow, revision, post, onNotice, onReady, debounceMs = 1000 }: {
  frameWindow: Window
  revision: string
  post: (body: AnswersBody) => void
  onNotice: (notice: 'needs-newer-console') => void
  onReady?: () => void
  debounceMs?: number
}) {
  let pending: AnswersBody | null = null
  // What the page shows: its last draft, or the set it was restored with.
  let latest: Answers = {}
  let timer: ReturnType<typeof setTimeout> | undefined

  const flush = () => {
    clearTimeout(timer)
    if (pending) post(pending)
    pending = null
  }

  return {
    handleMessage(e: MessageEvent) {
      if (e.source !== frameWindow) return
      const d = e.data as { type?: string; v?: number; answers?: Answers }
      if (d?.v !== 1) return onNotice('needs-newer-console')
      if (d.type === 'ready') return onReady?.()
      if (d.type !== 'answers') return
      latest = d.answers ?? {}
      pending = { answers: latest, submit: null, revision }
      clearTimeout(timer)
      timer = setTimeout(flush, debounceMs)
    },
    restore(answers: Answers) {
      latest = answers
      frameWindow.postMessage({ type: 'restore', v: 1, answers }, '*')
    },
    /** An operator's button press: the pending draft is part of it. */
    submit(submit: NonNullable<AnswersBody['submit']>) {
      clearTimeout(timer)
      pending = null
      post({ answers: latest, submit, revision })
    },
    flush,
  }
}
