import { afterEach, beforeEach, expect, it, vi } from 'vitest'

// Interface under test (lib/reviewBridge.ts):
//   createBridge({ frameWindow, revision, post, onNotice, onReady?, debounceMs })
//   -> { handleMessage(event), restore(answers), submit(kind), flush() }
// post receives { answers, submit, revision }. A frame message is always a
// draft; only submit(kind), the console's own buttons, posts a submission. restore posts
// { type: 'restore', v: 1, answers } to frameWindow. onReady fires on a v1 ready.
// A variable path: the import fails inside each test, not at collection or tsc.
const MODULE = '../reviewBridge'
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const load = async () => (await import(/* @vite-ignore */ MODULE)).createBridge as (o: Record<string, unknown>) => any

beforeEach(() => vi.useFakeTimers())
afterEach(() => vi.useRealTimers())

async function setup() {
  const createBridge = await load()
  const frameWindow = { postMessage: vi.fn() } as unknown as Window
  const post = vi.fn()
  const onNotice = vi.fn()
  const onReady = vi.fn()
  const bridge = createBridge({ frameWindow, revision: 'r1', post, onNotice, onReady, debounceMs: 1000 })
  const send = (data: unknown, source: unknown = frameWindow) =>
    bridge.handleMessage({ source, data } as unknown as MessageEvent)
  const answers = (a: object, submit: string | null = null, v = 1) => send({ type: 'answers', v, answers: a, submit })
  return { bridge, frameWindow, post, onNotice, onReady, send, answers }
}

it('ignores an event from another window', async () => {
  const { answers, send, post, onNotice, onReady } = await setup()
  answers({ format: 'a' })
  const other = {} as Window
  send({ type: 'answers', v: 1, answers: { format: 'a' }, submit: 'approve' }, other)
  send({ type: 'ready', v: 1 }, other)
  send({ type: 'answers', v: 2, answers: {}, submit: null }, other)
  vi.advanceTimersByTime(5000)
  expect(post).toHaveBeenCalledTimes(1) // only the first, from the frame
  expect(onNotice).not.toHaveBeenCalled()
  expect(onReady).not.toHaveBeenCalled()
})

it('ignores v other than 1 and raises the notice', async () => {
  const { answers, send, post, onNotice } = await setup()
  answers({ format: 'a' }, 'changes', 2)
  send({ type: 'ready', v: 2 })
  vi.advanceTimersByTime(5000)
  expect(post).not.toHaveBeenCalled()
  expect(onNotice).toHaveBeenCalledWith('needs-newer-console')
})

it('debounces drafts into one post with the last full set and the revision', async () => {
  const { answers, post } = await setup()
  answers({ format: 'a' })
  vi.advanceTimersByTime(400)
  answers({ format: 'b' })
  vi.advanceTimersByTime(400)
  answers({ format: 'b', track: 'security' })
  expect(post).not.toHaveBeenCalled()
  vi.advanceTimersByTime(999)
  expect(post).not.toHaveBeenCalled()
  vi.advanceTimersByTime(1)
  expect(post).toHaveBeenCalledTimes(1)
  expect(post).toHaveBeenCalledWith({ answers: { format: 'b', track: 'security' }, submit: null, revision: 'r1' })
  vi.advanceTimersByTime(5000)
  expect(post).toHaveBeenCalledTimes(1)
})

it('a submit in a frame message is ignored: the page can only send drafts', async () => {
  const { answers, post } = await setup()
  answers({ format: 'a' }, 'approve')
  answers({ format: 'b' }, 'changes')
  expect(post).not.toHaveBeenCalled()
  vi.advanceTimersByTime(1000)
  expect(post.mock.calls).toEqual([[{ answers: { format: 'b' }, submit: null, revision: 'r1' }]])
})

it('submit posts the last set of the page at once and drops the pending draft', async () => {
  const { bridge, answers, post } = await setup()
  answers({ format: 'a' })
  bridge.submit('changes')
  expect(post.mock.calls).toEqual([[{ answers: { format: 'a' }, submit: 'changes', revision: 'r1' }]])
  vi.advanceTimersByTime(5000)
  expect(post).toHaveBeenCalledTimes(1)
})

it('submit with no draft posts the restored set', async () => {
  const { bridge, post } = await setup()
  bridge.restore({ format: 'b' })
  bridge.submit('approve')
  expect(post.mock.calls).toEqual([[{ answers: { format: 'b' }, submit: 'approve', revision: 'r1' }]])
})

it('flush posts a pending draft once, and nothing when none is pending', async () => {
  const { bridge, answers, post } = await setup()
  bridge.flush()
  expect(post).not.toHaveBeenCalled()
  answers({ format: 'a' })
  bridge.flush()
  expect(post).toHaveBeenCalledTimes(1)
  expect(post).toHaveBeenCalledWith({ answers: { format: 'a' }, submit: null, revision: 'r1' })
  vi.advanceTimersByTime(5000)
  bridge.flush()
  expect(post).toHaveBeenCalledTimes(1)
})

it('calls onReady on ready and restore posts to the frame window', async () => {
  const { bridge, send, onReady, frameWindow } = await setup()
  send({ type: 'ready', v: 1 })
  expect(onReady).toHaveBeenCalledTimes(1)
  bridge.restore({ format: 'b' })
  expect(frameWindow.postMessage).toHaveBeenCalledWith({ type: 'restore', v: 1, answers: { format: 'b' } }, '*')
})
