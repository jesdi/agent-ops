import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { TaskCardView } from '../TaskCard'
import { inProgressCard, loginParkedCard, parkedCard, reviewCard } from '../../test/fixtures'
import { SLOT_COLORS } from '../../lib/capacity'

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

it('shows the backlog score when the card has one', () => {
  renderCard({ ...inProgressCard, score: 8.5 })
  expect(screen.getByText('score 8.5')).toBeInTheDocument()
})

it('omits the score badge when the card has no score', () => {
  renderCard({ ...inProgressCard, score: null })
  expect(screen.queryByText(/score/)).not.toBeInTheDocument()
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

test('active card shows time since claim; done card shows total cycle', () => {
  renderCard(
    { ...inProgressCard, claimed_at: new Date(Date.now() - 7200_000).toISOString() },
  )
  expect(screen.getByText(/claimed 2h ago/)).toBeInTheDocument()
  renderCard(
    { ...inProgressCard, stage: 'done', column: 'done', cycle_seconds: 8100 },
  )
  expect(screen.getByText(/took 2h 15m/)).toBeInTheDocument()
})

test('shows an envelope badge when messages are queued', () => {
  renderCard({ ...parkedCard, undelivered_messages: 2 })
  expect(screen.getByTestId('mail-badge')).toHaveTextContent('2')
})

test('no envelope badge when nothing is queued', () => {
  renderCard({ ...parkedCard, undelivered_messages: 0 })
  expect(screen.queryByTestId('mail-badge')).toBeNull()
})

test('a starved wake says so on the card', () => {
  renderCard({ ...parkedCard, wake_blocked: true })
  expect(screen.getByText('waiting for a free slot')).toBeInTheDocument()
})

test('a card holding a slot is bordered and chipped in that slot colour', () => {
  const { container } = render(
    <MemoryRouter>
      <TaskCardView card={{ ...inProgressCard, slot: 2 }} />
    </MemoryRouter>)
  expect(screen.getByTestId('slot-chip')).toHaveTextContent('slot 2')
  expect(container.firstElementChild?.className).toContain(SLOT_COLORS[2])
})

test('a slot-less card carries no slot chip', () => {
  render(
    <MemoryRouter>
      <TaskCardView card={{ ...reviewCard, slot: -1 }} />
    </MemoryRouter>)
  expect(screen.queryByTestId('slot-chip')).toBeNull()
})
