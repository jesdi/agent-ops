import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { UsageBanner } from '../UsageBanner'
import type { UsageView } from '../../lib/api'
import { banner } from '../../lib/tone'
import { usage, usageUnavailable } from '../../test/fixtures'
import { server } from '../../test/msw-server'
import { renderWithProviders as render } from '../../test/render'

const SINCE = '2026-11-05T10:00:00+00:00'
const authDark: UsageView = { ...usageUnavailable, auth_dark: { since: SINCE } }
// Gate closed by a binding window, 130 minutes to reset.
const overPace: UsageView = {
  ...usage,
  gate: { ...usage.gate, admitted: false, minutes_to_reset: 130 },
}

const serve = (body: UsageView) => server.use(http.get('/api/usage', () => HttpResponse.json(body)))
const classesOf = (cls: string) => cls.split(' ')

it('auth_dark shows the failed-tone alert with the login copy and the local since line', async () => {
  serve(authDark)
  render(<UsageBanner />)
  const alert = await screen.findByTestId('auth-dark-banner')
  expect(alert).toHaveAttribute('role', 'alert')
  expect(alert).toHaveClass(...classesOf(banner.failed))
  expect(alert).toHaveTextContent("Can't read usage: the provider login has probably expired")
  expect(screen.getByText(`since ${new Date(SINCE).toLocaleString()}`)).toBeInTheDocument()
  expect(screen.queryByTestId('usage-exhausted-banner')).not.toBeInTheDocument()
})

it('auth_dark wins over a closed gate with a binding window', async () => {
  serve({ ...overPace, auth_dark: { since: SINCE } })
  render(<UsageBanner />)
  await screen.findByTestId('auth-dark-banner')
  expect(screen.queryByTestId('usage-exhausted-banner')).not.toBeInTheDocument()
  expect(screen.getAllByRole('alert')).toHaveLength(1)
})

it('a closed gate with a binding window shows the waiting-tone over-pace alert', async () => {
  serve(overPace)
  render(<UsageBanner />)
  const alert = await screen.findByTestId('usage-exhausted-banner')
  expect(alert).toHaveAttribute('role', 'alert')
  expect(alert).toHaveClass(...classesOf(banner.waiting))
  expect(alert).toHaveTextContent(/^Usage over pace: claims paused\. The window resets in 2h 10m$/)
  expect(alert.textContent).not.toContain('until')
  expect(screen.queryByTestId('auth-dark-banner')).not.toBeInTheDocument()
})

it('a closed gate without a binding window renders nothing', async () => {
  let asked = false
  server.use(http.get('/api/usage', () => { asked = true; return HttpResponse.json(usageUnavailable) }))
  render(<UsageBanner />)
  await waitFor(() => expect(asked).toBe(true))
  await new Promise((r) => setTimeout(r, 50))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('an admitted gate renders nothing', async () => {
  let asked = false
  server.use(http.get('/api/usage', () => { asked = true; return HttpResponse.json(usage) }))
  render(<UsageBanner />)
  await waitFor(() => expect(asked).toBe(true))
  await new Promise((r) => setTimeout(r, 50))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('an errored usage query renders nothing', async () => {
  let asked = false
  server.use(http.get('/api/usage', () => {
    asked = true
    return HttpResponse.json({ detail: 'usage source exploded' }, { status: 500 })
  }))
  const { queryClient } = render(<UsageBanner />)
  await waitFor(() => expect(asked).toBe(true))
  await waitFor(() => expect(queryClient.getQueryState(['usage'])?.status).toBe('error'))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})

it('the banner vanishes when the usage query refetches with the marker cleared', async () => {
  serve(authDark)
  const { queryClient } = render(<UsageBanner />)
  await screen.findByTestId('auth-dark-banner')
  serve(usage)
  await queryClient.invalidateQueries({ queryKey: ['usage'] })
  await waitFor(() => expect(screen.queryByTestId('auth-dark-banner')).not.toBeInTheDocument())
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
