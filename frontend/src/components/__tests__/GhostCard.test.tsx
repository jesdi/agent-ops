import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { vi } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { GhostCardView } from '../GhostCard'
import type { GhostCard } from '../../lib/api'
import { server } from '../../test/msw-server'

const ghost: GhostCard = {
  number: 73, target: 'widget', title: 'Ship dark mode',
  url: 'https://github.com/jesdi/widget/issues/73', score: 8.5, boost: 2,
}

test('renders muted upcoming card with rank data and links to the task view', () => {
  renderWithProviders(
    <GhostCardView ghost={ghost} isNext={false} busy={false}
      onBoost={() => {}} onNext={() => {}} onReady={() => {}} />,
  )
  const card = screen.getByTestId('ghost-73')
  expect(card).toHaveTextContent('Ship dark mode')
  expect(card).toHaveTextContent('score 8.5')
  expect(card).toHaveTextContent('boost 2')
  expect(screen.getByRole('link')).toHaveAttribute('href', '/task/widget/73')
  expect(screen.queryByText('next')).not.toBeInTheDocument()
})

test('next badge and actions', async () => {
  const onBoost = vi.fn()
  renderWithProviders(
    <GhostCardView ghost={ghost} isNext busy={false}
      onBoost={onBoost} onNext={() => {}} onReady={() => {}} />,
  )
  expect(screen.getByText('next')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Boost' }))
  expect(onBoost).toHaveBeenCalledWith(73, 1)
  await userEvent.click(screen.getByRole('button', { name: 'Demote' }))
  expect(onBoost).toHaveBeenCalledWith(73, -1)
})

test('a capacity-blocked queued card can force its first claim', async () => {
  let posted: unknown = null
  server.use(http.post('/api/task/widget/73/run', async ({ request }) => {
    posted = await request.json()
    return HttpResponse.json({ ok: true, reason: 'forced' })
  }))
  renderWithProviders(
    <GhostCardView ghost={{
      ...ghost,
      admission: {
        requested: {
          model: 'claude-fable-5', provider: 'anthropic', admitted: false,
          note: 'Fable weekly capacity is low',
        },
        alternatives: [],
      },
    }} isNext busy={false} onBoost={() => {}} onNext={() => {}} onReady={() => {}} />,
  )
  await userEvent.click(screen.getByRole('button', { name: /fable-5 capacity limited/i }))
  await userEvent.click(screen.getByRole('button', { name: /run anyway with fable-5/i }))
  await waitFor(() => expect(posted).toEqual({
    model: 'claude-fable-5', bypass_usage: true,
  }))
})
