import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { TaskCardView } from '../TaskCard'
import { parkedCard, reviewCard } from '../../test/fixtures'

function renderCard(card: typeof parkedCard, pendingActions?: string[]) {
  return render(
    <MemoryRouter>
      <TaskCardView card={card} pendingActions={pendingActions} />
    </MemoryRouter>,
  )
}

it('renders slot number for a slotted card', () => {
  renderCard(parkedCard)
  expect(screen.getByText('slot 1')).toBeInTheDocument()
})

it('does not render slot chip for a gate-parked card (slot = -1)', () => {
  renderCard(reviewCard)
  expect(screen.queryByText(/slot/)).not.toBeInTheDocument()
})

it('shows parked badge when park is set', () => {
  renderCard(reviewCard)
  expect(screen.getByText('parked: awaiting-review')).toBeInTheDocument()
})
