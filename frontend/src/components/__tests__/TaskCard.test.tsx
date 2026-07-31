import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { TaskCardView } from '../TaskCard'
import { inProgressCard, loginParkedCard, parkedCard, reviewCard } from '../../test/fixtures'

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

it('shows the feedback-queued badge when feedback_pending', () => {
  renderCard({ ...parkedCard, feedback_pending: true })
  expect(screen.getByText('feedback queued')).toBeInTheDocument()
})

it('hides the badge otherwise', () => {
  renderCard({ ...parkedCard, feedback_pending: false })
  expect(screen.queryByText('feedback queued')).not.toBeInTheDocument()
})

it('marks a card that holds a capacity unit, in text and not only colour', () => {
  renderCard({ ...inProgressCard, consuming_capacity: true })
  expect(screen.getByText('holding a capacity unit')).toBeInTheDocument()
})

it('leaves a card that holds no unit unmarked', () => {
  renderCard({ ...parkedCard, consuming_capacity: false })
  expect(screen.queryByText('holding a capacity unit')).not.toBeInTheDocument()
})

it('AWKWARD: a login-parked card is marked — parked, yet still consuming', () => {
  renderCard(loginParkedCard)
  expect(screen.getByText('parked: parked-login')).toBeInTheDocument()
  expect(screen.getByText('holding a capacity unit')).toBeInTheDocument()
})

// The accent border is purely visual with no accessible surface of its own;
// its text counterpart ('holding a capacity unit') is asserted separately above.
// These class assertions exist specifically to catch a regressed ACCENT_BORDER
// application — the sr-only span is gated independently and would pass without it.
it('accent: consuming card gets blue border classes by default', () => {
  renderCard({ ...inProgressCard, consuming_capacity: true })
  expect(screen.getByTestId('card-41')).toHaveClass('border-l-4', 'border-l-blue-500')
})

it('accent: non-consuming card has no accent border', () => {
  renderCard({ ...parkedCard, consuming_capacity: false })
  expect(screen.getByTestId('card-42')).not.toHaveClass('border-l-4')
})

it('accent: amber accent is applied when passed explicitly', () => {
  render(
    <MemoryRouter>
      <TaskCardView card={{ ...inProgressCard, consuming_capacity: true }} accent="amber" />
    </MemoryRouter>,
  )
  expect(screen.getByTestId('card-41')).toHaveClass('border-l-4', 'border-l-amber-500')
})
