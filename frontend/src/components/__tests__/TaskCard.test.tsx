import { fireEvent, screen, waitFor } from '@testing-library/react'
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

async function renderExpanded(card: typeof parkedCard) {
  const view = renderCard(card)
  await userEvent.click(screen.getByRole('button', { name: `Details for ${card.target}#${card.issue}` }))
  return view
}

it('compact card shows identifier, title link and updated stamp, and only the signals that apply', () => {
  const card = {
    ...inProgressCard, updated_at: new Date(Date.now() - 300_000).toISOString(),
    undelivered_messages: 0, wake_blocked: false, feedback_pending: false, park_note_pending: false,
  }
  renderCard(card, ['cancel'])
  const article = screen.getByTestId('card-41')
  expect(article).toHaveTextContent('widget#41')
  expect(screen.getByRole('link', { name: card.title })).toHaveAttribute('href', '/task/widget/41')
  expect(screen.getByText('5m ago')).toBeInTheDocument()
  expect(screen.getByTestId('pending-badge')).toHaveTextContent('pending: cancel')
  for (const signal of ['feedback queued', 'notify pending', 'waiting for a free slot', /parked:/]) {
    expect(screen.queryByText(signal)).toBeNull()
  }
  expect(screen.queryByTestId('mail-badge')).toBeNull()
  expect(screen.queryByText(card.model)).toBeNull()
})

it('parked cards show the park reason and in-progress cards the stage while compact', () => {
  renderCard(parkedCard)
  expect(screen.getByText('parked: question')).toBeInTheDocument()
  expect(screen.queryByText('Implementing')).toBeNull()
  renderCard(inProgressCard)
  expect(screen.getByText('Implementing')).toBeInTheDocument()
})

it('the expanded detail block names the stage on every card, not only In progress', async () => {
  await renderExpanded(parkedCard)
  const chevron = screen.getByRole('button', { name: 'Details for widget#42' })
  expect(document.getElementById(chevron.getAttribute('aria-controls')!)).toHaveTextContent('Implementing')
})

it('the title clamps to two lines while compact and unclamps when expanded', async () => {
  renderCard(inProgressCard)
  const link = screen.getByRole('link', { name: inProgressCard.title })
  // `block` would override the clamp's display, so it must not ride along.
  expect(link).toHaveClass('line-clamp-2')
  expect(link).not.toHaveClass('block')
  await userEvent.click(screen.getByRole('button', { name: 'Details for widget#41' }))
  expect(link).not.toHaveClass('line-clamp-2')
})

it('the chevron expands the detail block and flips aria-expanded', async () => {
  renderCard({ ...inProgressCard, track: 'security' })
  const chevron = screen.getByRole('button', { name: 'Details for widget#41' })
  expect(chevron).toHaveAttribute('aria-expanded', 'false')
  expect(screen.queryByText('track security')).toBeNull()
  await userEvent.click(chevron)
  expect(chevron).toHaveAttribute('aria-expanded', 'true')
  expect(document.getElementById(chevron.getAttribute('aria-controls')!)).toHaveTextContent(
    'track security',
  )
  await userEvent.click(chevron)
  expect(screen.queryByText('track security')).toBeNull()
})

it('a touch tap on the body expands; a mouse click on the body does not', () => {
  renderCard({ ...inProgressCard, track: 'security' })
  const article = screen.getByTestId('card-41')
  fireEvent.pointerUp(article, { pointerType: 'mouse' })
  expect(screen.queryByText('track security')).toBeNull()
  fireEvent.pointerUp(article, { pointerType: 'touch' })
  expect(screen.getByText('track security')).toBeInTheDocument()
})

it('a touch tap on the title link navigates rather than expands', () => {
  renderCard({ ...inProgressCard, track: 'security' })
  fireEvent.pointerUp(screen.getByRole('link'), { pointerType: 'touch' })
  expect(screen.queryByText('track security')).toBeNull()
})

it('the whole card is the drag source carrying the cancel payload', () => {
  renderCard(inProgressCard)
  const data: Record<string, string> = {}
  fireEvent.dragStart(screen.getByTestId('card-41'), {
    dataTransfer: { setData: (type: string, value: string) => { data[type] = value } },
  })
  expect(JSON.parse(data['application/x-agent-ops-card']!)).toEqual(
    { issue: 41, target: 'widget', title: inProgressCard.title },
  )
})

it('renders slot number for a slotted card', async () => {
  await renderExpanded(parkedCard)
  expect(screen.getByText('slot 1')).toBeInTheDocument()
})

it('does not render slot chip for a gate-parked card (slot = -1)', async () => {
  await renderExpanded(reviewCard)
  expect(screen.queryByText(/slot/)).not.toBeInTheDocument()
})

it('the column explains a non-Parked park, so the reason stays off the card', () => {
  renderCard(reviewCard)
  expect(screen.queryByText(/parked:/)).toBeNull()
})

it('shows the backlog score when the card has one', async () => {
  await renderExpanded({ ...inProgressCard, score: 8.5 })
  expect(screen.getByText('score 8.5')).toBeInTheDocument()
})

it('omits the score badge when the card has no score', async () => {
  await renderExpanded({ ...inProgressCard, score: null })
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

test('active card shows time since claim; done card shows total cycle', async () => {
  await renderExpanded(
    { ...inProgressCard, claimed_at: new Date(Date.now() - 7200_000).toISOString() },
  )
  expect(screen.getByText(/claimed 2h ago/)).toBeInTheDocument()
  await renderExpanded(
    { ...inProgressCard, issue: 99, stage: 'done', column: 'done', cycle_seconds: 8100 },
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

test('a card holding a slot is bordered and chipped in that slot colour', async () => {
  const { container } = await renderExpanded({ ...inProgressCard, slot: 2 })
  expect(screen.getByTestId('slot-chip')).toHaveTextContent('slot 2')
  expect(container.firstElementChild?.className).toContain(SLOT_COLORS[2])
})

test('a slot-less card carries no slot chip', async () => {
  await renderExpanded({ ...reviewCard, slot: -1 })
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
      any_provider: false,
    },
  })

  await userEvent.click(screen.getByRole('button', { name: /fable-5 capacity limited/i }))
  expect(screen.getByRole('dialog', { name: 'Model capacity options' })).toHaveTextContent(
    '46% used, allowance 41%',
  )
  await userEvent.click(screen.getByRole('button', { name: /run with opus-4-8/i }))
  await waitFor(() => expect(posted).toEqual({ model: 'claude-opus-4-8' }))
})

it('shows the track chip', async () => {
  await renderExpanded({ ...inProgressCard, track: 'security' })
  expect(screen.getByText('track security')).toBeInTheDocument()
})
