import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { UsagePanel } from '../UsagePanel'
import { useUsage } from '../../hooks/useResources'
import type { UsageView } from '../../lib/api'
import { usageRouted } from '../../test/fixtures'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'

// Expected shape: a radiogroup named /priority/i holding exactly three
// radios named Auto, Anthropic, OpenAI; the selected one is aria-checked.

/** The panel fed the way the board feeds it: from the usage query. */
function Live() {
  const { data } = useUsage()
  return data ? <UsagePanel usage={data} /> : null
}

/** A server that remembers the mode, like the real one; records POST bodies. */
function serve(initial: UsageView = usageRouted, refuse?: string) {
  let view = initial
  const posts: unknown[] = []
  server.use(
    http.get('/api/usage', () => HttpResponse.json(view)),
    http.post('/api/priority', async ({ request }) => {
      const body = (await request.json()) as { mode: string }
      posts.push(body)
      if (refuse) return HttpResponse.json({ detail: refuse }, { status: 422 })
      view = { ...view, priority: { ...view.priority, mode: body.mode, first: body.mode === 'auto' ? view.priority.first : body.mode } }
      return HttpResponse.json(view.priority)
    }),
  )
  return posts
}

const mode = (mode: string): UsageView => ({ ...usageRouted, priority: { ...usageRouted.priority, mode } })

async function group() {
  return screen.findByRole('radiogroup', { name: /priority/i })
}
const radio = (g: HTMLElement, name: string) => within(g).getByRole('radio', { name })

it('offers Auto, Anthropic and OpenAI with Auto selected in auto mode', async () => {
  serve()
  renderWithProviders(<Live />)
  const g = await group()
  expect(within(g).getAllByRole('radio').map((r) => r.getAttribute('aria-label') ?? r.textContent))
    .toEqual(['Auto', 'Anthropic', 'OpenAI'])
  expect(radio(g, 'Auto')).toBeChecked()
  expect(radio(g, 'Anthropic')).not.toBeChecked()
  expect(radio(g, 'OpenAI')).not.toBeChecked()
})

it('selects OpenAI in openai mode', async () => {
  serve(mode('openai'))
  renderWithProviders(<Live />)
  const g = await group()
  expect(radio(g, 'OpenAI')).toBeChecked()
  expect(radio(g, 'Auto')).not.toBeChecked()
})

it('choosing OpenAI posts {"mode":"openai"} and then selects it', async () => {
  const posts = serve()
  renderWithProviders(<Live />)
  const g = await group()
  await userEvent.click(radio(g, 'OpenAI'))
  await waitFor(() => expect(posts).toEqual([{ mode: 'openai' }]))
  await waitFor(() => expect(radio(screen.getByRole('radiogroup', { name: /priority/i }), 'OpenAI')).toBeChecked())
})

it('a refused change keeps the selection and shows the error', async () => {
  serve(usageRouted, 'openai is not routable here')
  renderWithProviders(<Live />)
  const g = await group()
  await userEvent.click(radio(g, 'OpenAI'))
  expect(await screen.findByText(/openai is not routable here/)).toBeInTheDocument()
  expect(radio(screen.getByRole('radiogroup', { name: /priority/i }), 'Auto')).toBeChecked()
  expect(radio(screen.getByRole('radiogroup', { name: /priority/i }), 'OpenAI')).not.toBeChecked()
})

it('only the provider reported first carries the "first" chip', () => {
  const { unmount } = renderWithProviders(<UsagePanel usage={usageRouted} />)
  expect(screen.getAllByText('first')).toHaveLength(1)
  expect(within(screen.getByText('anthropic').closest('div')!).getByText('first')).toBeInTheDocument()
  unmount()
  renderWithProviders(<UsagePanel usage={{ ...usageRouted, priority: { ...usageRouted.priority, first: 'openai' } }} />)
  expect(screen.getAllByText('first')).toHaveLength(1)
  expect(within(screen.getByText('openai').closest('div')!).getByText('first')).toBeInTheDocument()
})

it('weekly windows show their required pace to one decimal; session and null show none', () => {
  renderWithProviders(<UsagePanel usage={usageRouted} />)
  // 5.6 on anthropic week, 1.0 on openai week; the anthropic session and the
  // Fable week (required_pace null) add none.
  expect(screen.getAllByText(/pace/)).toHaveLength(2)
  expect(screen.getByText('5.6× pace')).toBeInTheDocument()
  expect(screen.getByText('1.0× pace')).toBeInTheDocument()
})

it('a session window shows no pace text even when it carries a number', () => {
  const p = usageRouted.providers[0]!
  const session = { ...p.windows[0]!, required_pace: 2.0 }
  renderWithProviders(<UsagePanel usage={{ ...usageRouted, providers: [{ ...p, windows: [session] }] }} />)
  expect(screen.queryByText(/pace/)).not.toBeInTheDocument()
})

it('headroom, allowance and remaining text read as before', () => {
  renderWithProviders(<UsagePanel usage={usageRouted} />)
  expect(screen.getByText('cap 80%')).toBeInTheDocument()
  expect(screen.getByText('95% left')).toBeInTheDocument()
  expect(screen.getByText('headroom 15.9 pts')).toBeInTheDocument()
  expect(screen.getByText('87% left')).toBeInTheDocument()
  expect(screen.getByText('headroom 5.9 pts')).toBeInTheDocument()
  expect(screen.getByText('77% left')).toBeInTheDocument()
  expect(screen.getByText('headroom 8.9 pts')).toBeInTheDocument()
  expect(screen.getByText('80% left')).toBeInTheDocument()
  const bars = screen.getAllByRole('progressbar')
  expect(bars[1]).toHaveAttribute('aria-valuetext', '13% used of 29% allowed now, 87% remaining, on pace')
  expect(bars[2]).toHaveAttribute('aria-valuetext', '23% used of 29% allowed now, 77% remaining, close to the limit')
  expect(bars[3]).toHaveAttribute('aria-valuetext', '20% used of 29% allowed now, 80% remaining, on pace')
})

it('the control is keyboard operable and exposes its selection', async () => {
  const posts = serve()
  renderWithProviders(<Live />)
  const g = await group()
  await userEvent.tab()
  expect(g.contains(document.activeElement)).toBe(true)
  expect(document.activeElement).toBe(radio(g, 'Auto')) // the selected radio is the tab stop
  await userEvent.keyboard('{ArrowRight}{ArrowRight}')
  expect(document.activeElement).toBe(radio(g, 'OpenAI'))
  await userEvent.keyboard(' ')
  await waitFor(() => expect(posts).toEqual([{ mode: 'openai' }]))
  await waitFor(() => expect(radio(screen.getByRole('radiogroup', { name: /priority/i }), 'OpenAI')).toBeChecked())
})
