import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'
import { renderWithProviders } from '../../test/render'
import { GhostCardView } from '../GhostCard'
import type { GhostCard } from '../../lib/api'

const ghost: GhostCard = {
  number: 73, target: 'jesdi/widget', title: 'Ship dark mode',
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
  expect(screen.getByRole('link')).toHaveAttribute('href', '/task/73')
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
