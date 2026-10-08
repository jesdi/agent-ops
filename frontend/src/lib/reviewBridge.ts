import type { components } from './api-types'

export type AnswersBody = components['schemas']['AnswersReq']
type Answers = AnswersBody['answers']

/** The console side of the review page protocol (v 1). It trusts a message only
 *  when it comes from the iframe's window, debounces drafts, and posts a
 *  submission at once with the revision the page was loaded on. */
export function createBridge({ frameWindow, revision, post, onNotice, onReady, debounceMs = 1000 }: {
  frameWindow: Window
  revision: string
  post: (body: AnswersBody) => void
  onNotice: (notice: 'needs-newer-console') => void
  onReady?: () => void
  debounceMs?: number
}) {
  let pending: AnswersBody | null = null
  let timer: ReturnType<typeof setTimeout> | undefined

  const flush = () => {
    clearTimeout(timer)
    if (pending) post(pending)
    pending = null
  }

  return {
    handleMessage(e: MessageEvent) {
      if (e.source !== frameWindow) return
      const d = e.data as { type?: string; v?: number; answers?: Answers; submit?: AnswersBody['submit'] }
      if (d?.v !== 1) return onNotice('needs-newer-console')
      if (d.type === 'ready') return onReady?.()
      if (d.type !== 'answers') return
      pending = { answers: d.answers ?? {}, submit: d.submit ?? null, revision }
      if (d.submit) return flush()
      clearTimeout(timer)
      timer = setTimeout(flush, debounceMs)
    },
    restore(answers: unknown) {
      frameWindow.postMessage({ type: 'restore', v: 1, answers }, '*')
    },
    flush,
  }
}
