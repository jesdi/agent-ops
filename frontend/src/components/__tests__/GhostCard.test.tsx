import { fireEvent, screen, waitFor } from '@testing-library/react'
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

function expand(id = 'widget#73') {
  return userEvent.click(screen.getByRole('button', { name: `Details for ${id}` }))
}

test('compact ghost shows identifier and title link, with rank data behind expand', async () => {
  renderWithProviders(
    <GhostCardView ghost={ghost} isNext={false} busy={false}
      onBoost={() => {}} onNext={() => {}} onReady={() => {}} />,
  )
  const card = screen.getByTestId('ghost-73')
  expect(card).toHaveTextContent('widget#73')
  expect(screen.getByRole('link')).toHaveAttribute('href', '/task/widget/73')
  expect(screen.getByRole('link')).toHaveTextContent('Ship dark mode')
  expect(screen.queryByText('next')).not.toBeInTheDocument()
  expect(card).not.toHaveTextContent('score 8.5')
  expect(screen.queryByRole('button', { name: 'Boost' })).not.toBeInTheDocument()

  await expand()
  expect(card).toHaveTextContent('score 8.5')
  expect(card).toHaveTextContent('boost 2')
  // Expand adds no second link.
  expect(screen.getByRole('link')).toHaveAttribute('href', '/task/widget/73')
})

test('compact ghost shows the next badge', () => {
  renderWithProviders(
    <GhostCardView ghost={ghost} isNext busy={false}
      onBoost={() => {}} onNext={() => {}} onReady={() => {}} />,
  )
  expect(screen.getByText('next')).toBeInTheDocument()
})

test('ranking actions appear only when expanded and still act', async () => {
  const onBoost = vi.fn()
  const onNext = vi.fn()
  const onReady = vi.fn()
  renderWithProviders(
    <GhostCardView ghost={ghost} isNext busy={false}
      onBoost={onBoost} onNext={onNext} onReady={onReady} />,
  )
  for (const name of ['Boost', 'Demote', 'Next', 'Ready']) {
    expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
  }
  await expand()
  await userEvent.click(screen.getByRole('button', { name: 'Boost' }))
  expect(onBoost).toHaveBeenCalledWith(73, 1)
  await userEvent.click(screen.getByRole('button', { name: 'Demote' }))
  expect(onBoost).toHaveBeenCalledWith(73, -1)
  await userEvent.click(screen.getByRole('button', { name: 'Next' }))
  expect(onNext).toHaveBeenCalledWith(73)
  await userEvent.click(screen.getByRole('button', { name: 'Ready' }))
  expect(onReady).toHaveBeenCalledWith(73)
})

test('busy disables every ranking action', async () => {
  renderWithProviders(
    <GhostCardView ghost={ghost} isNext={false} busy
      onBoost={() => {}} onNext={() => {}} onReady={() => {}} />,
  )
  await expand()
  for (const name of ['Boost', 'Demote', 'Next', 'Ready']) {
    expect(screen.getByRole('button', { name })).toBeDisabled()
  }
})

test('a touch tap on the ghost body expands it', () => {
  renderWithProviders(
    <GhostCardView ghost={ghost} isNext={false} busy={false}
      onBoost={() => {}} onNext={() => {}} onReady={() => {}} />,
  )
  fireEvent.pointerUp(screen.getByTestId('ghost-73'), { pointerType: 'touch' })
  expect(screen.getByRole('button', { name: 'Boost' })).toBeInTheDocument()
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
  expect(screen.queryByRole('button', { name: /fable-5 capacity limited/i })).not.toBeInTheDocument()
  await expand()
  await userEvent.click(screen.getByRole('button', { name: /fable-5 capacity limited/i }))
  await userEvent.click(screen.getByRole('button', { name: /run anyway with fable-5/i }))
  await waitFor(() => expect(posted).toEqual({
    model: 'claude-fable-5', bypass_usage: true,
  }))
})
