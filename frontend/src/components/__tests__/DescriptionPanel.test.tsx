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
  // findByRole's internal waitFor wraps polls in async act, which flushes React
  // effects AND drains the microtask queue — including MSW response promises.
  // So by the time findByRole resolves, any eager fetch has fully completed and
  // the result is in the cache. A disabled query (enabled=false) never fetches,
  // so getQueryData stays undefined. This is deterministic: no timing dependency.
  await screen.findByRole('button', { name: /description/i })
  expect(queryClient.getQueryData(queryKeys.description(73))).toBeUndefined()

  await userEvent.click(screen.getByRole('button', { name: /description/i }))
  expect(await screen.findByText('Everything dark.')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Goal' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /github/i })).toHaveAttribute('href', desc.url)
  // After expanding: data is in cache, proving the fetch happened after the click.
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
