import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { queryKeys } from '../../hooks/queryKeys'
import { renderWithProviders } from '../../test/render'
import { DescriptionPanel } from '../DescriptionPanel'

const desc = {
  title: 'Ship dark mode', body: '## Goal\nEverything dark.',
  url: 'https://github.com/jesdi/widget/issues/73',
  fetched_at: '2026-08-01T12:00:00Z', error: '',
}

test('collapsed by default; fetches only on expand; renders markdown', async () => {
  server.use(http.get('/api/task/73/description', () => HttpResponse.json(desc)))
  const { queryClient } = renderWithProviders(<DescriptionPanel issue={73} defaultOpen={false} />)
  // fetchStatus flips to 'fetching' synchronously during the observer's useEffect,
  // which RTL's render-wrapping act() flushes before returning. This happens before
  // any MSW response arrives (proven: adding delay(50) to the handler — a macrotask
  // act cannot drain — still shows fetchStatus='fetching' and the toBe('idle') check
  // still fails red). A disabled query (enabled=false) never starts a fetch, so its
  // fetchStatus stays 'idle'. This check is therefore independent of MSW timing.
  expect(queryClient.getQueryState(queryKeys.description(73))?.fetchStatus).toBe('idle')

  const btn = await screen.findByRole('button', { name: /description/i })
  expect(btn).toHaveAttribute('aria-expanded', 'false')

  await userEvent.click(btn)
  expect(await screen.findByText('Everything dark.')).toBeInTheDocument()
  expect(btn).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByRole('heading', { name: 'Goal' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /github/i })).toHaveAttribute('href', desc.url)
  // After expanding, data must be in cache (fetch ran after the click, not before).
  expect(queryClient.getQueryData(queryKeys.description(73))).toBeDefined()
})

test('backend error payload renders as an explicit message', async () => {
  server.use(http.get('/api/task/73/description', () =>
    HttpResponse.json({ ...desc, title: '', body: '', url: '', error: 'gh: timeout' })))
  renderWithProviders(<DescriptionPanel issue={73} defaultOpen />)
  expect(await screen.findByText(/description unavailable: gh: timeout/)).toBeInTheDocument()
})

test('transport failure renders explicit error, not a blank panel', async () => {
  server.use(http.get('/api/task/73/description', () =>
    HttpResponse.json({ detail: 'gh: unreachable' }, { status: 500 })))
  renderWithProviders(<DescriptionPanel issue={73} defaultOpen />)
  expect(await screen.findByText(/description unavailable: gh: unreachable/)).toBeInTheDocument()
})

test('empty body renders the no-description fallback, not a blank panel', async () => {
  server.use(http.get('/api/task/73/description', () =>
    HttpResponse.json({ ...desc, body: '', error: '' })))
  renderWithProviders(<DescriptionPanel issue={73} defaultOpen />)
  // ReactMarkdown renders _no description_ as <em>no description</em>
  expect(await screen.findByText(/no description/)).toBeInTheDocument()
})

test('an ambiguous issue number degrades to explicit unavailable text', async () => {
  // The description route now answers 409 when the same number sits on two
  // targets' queues, rather than serving the first repo's body. The panel
  // must say so rather than render a blank or a wrong description.
  server.use(http.get('/api/task/73/description', () =>
    HttpResponse.json({ detail: 'issue 73 is ambiguous across targets' },
      { status: 409 })))
  renderWithProviders(<DescriptionPanel issue={73} defaultOpen />)
  expect(await screen.findByText(/description unavailable/)).toBeInTheDocument()
})
