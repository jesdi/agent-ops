import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'
import { DescriptionPanel } from '../DescriptionPanel'

const desc = {
  title: 'Ship dark mode', body: '## Goal\nEverything dark.',
  url: 'https://github.com/jesdi/widget/issues/73',
  fetched_at: '2026-08-01T12:00:00Z', error: '',
}

test('collapsed by default; fetches only on expand; renders markdown', async () => {
  let calls = 0
  server.use(http.get('/api/task/73/description', () => {
    calls += 1
    return HttpResponse.json(desc)
  }))
  renderWithProviders(<DescriptionPanel issue={73} defaultOpen={false} />)
  // findByRole flushes React effects (including React Query's useEffect that
  // would initiate an enabled query). If enabled=true, MSW intercepts the
  // fetch synchronously (calls++ fires before the response Promise resolves),
  // so calls would be >0 here. With enabled=false, calls stays 0.
  const btn = await screen.findByRole('button', { name: /description/i })
  expect(calls).toBe(0)
  await userEvent.click(btn)
  expect(await screen.findByText('Everything dark.')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Goal' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: /github/i })).toHaveAttribute('href', desc.url)
  // Exactly one fetch happened, and it happened after the click (not before).
  expect(calls).toBe(1)
})

test('backend error payload renders as an explicit message', async () => {
  server.use(http.get('/api/task/73/description', () =>
    HttpResponse.json({ ...desc, title: '', body: '', url: '', error: 'gh: timeout' })))
  renderWithProviders(<DescriptionPanel issue={73} defaultOpen />)
  expect(await screen.findByText(/description unavailable: gh: timeout/)).toBeInTheDocument()
})
