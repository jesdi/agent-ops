import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { TaskCardView } from '../TaskCard'
import { inProgressCard, loginParkedCard, parkedCard, reviewCard } from '../../test/fixtures'
import { SLOT_COLORS } from '../../lib/capacity'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'

function renderCard(card: typeof parkedCard, pendingActions?: string[]) {
  return renderWithProviders(<TaskCardView card={card} pendingActions={pendingActions} />)
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
  const { container } = renderCard({ ...inProgressCard, slot: 2 })
  expect(screen.getByTestId('slot-chip')).toHaveTextContent('slot 2')
  expect(container.firstElementChild?.className).toContain(SLOT_COLORS[2])
})

test('a slot-less card carries no slot chip', () => {
  renderCard({ ...reviewCard, slot: -1 })
  expect(screen.queryByTestId('slot-chip')).toBeNull()
})

test('model-capacity warning explains the block and can choose another model', async () => {
  let posted: unknown = null
  server.use(http.post('/api/task/widget/44/run', async ({ request }) => {
    posted = await request.json()
    return HttpResponse.json({ status: 'pending', intent: 'resume-44' }, { status: 202 })
  }))
  renderCard({
    ...reviewCard,
    park: 'unpark-requested',
    column: 'resuming',
    admission: {
      requested: {
        model: 'claude-fable-5', provider: 'anthropic', admitted: false,
        note: 'anthropic week·Fable: 46% used, allowance 41%, headroom −5 pts, resets in 4d 3h',
      },
      alternatives: [{
        model: 'claude-opus-4-8', provider: 'anthropic', admitted: true,
        note: 'anthropic week: capacity available',
      }],
    },
  })

  await userEvent.click(screen.getByRole('button', { name: /fable-5 capacity limited/i }))
  expect(screen.getByRole('dialog', { name: 'Model capacity options' })).toHaveTextContent(
    '46% used, allowance 41%',
  )
  await userEvent.click(screen.getByRole('button', { name: /run with opus-4-8/i }))
  await waitFor(() => expect(posted).toEqual({ model: 'claude-opus-4-8' }))
})

it('shows the track chip', () => {
  renderCard({ ...inProgressCard, track: 'security' })
  expect(screen.getByText('track security')).toBeInTheDocument()
})
