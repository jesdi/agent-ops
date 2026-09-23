import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import {
  board as fx_board, usageUnavailable, inProgressCard, pendingReplyIntent,
} from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import type { UsageView } from '../../lib/api'
import { BoardPage } from '../BoardPage'

beforeEach(() => server.use(...defaultHandlers))

const testIds = (els: HTMLElement[]) => els.map((el) => el.getAttribute('data-testid'))

it('occupied columns render in the row, empty ones as chips, never both', async () => {
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('column-parked')).toBeInTheDocument(),
  )
  expect(testIds(screen.getAllByTestId(/^column-/)))
    .toEqual(['column-parked', 'column-in-progress', 'column-awaiting-ci'])
  expect(testIds(screen.getAllByTestId(/^chip-/))).toEqual([
    'chip-needs-review', 'chip-pr-open', 'chip-failed', 'chip-stalled',
    'chip-queued', 'chip-resuming', 'chip-done', 'chip-wont-do'])
  expect(screen.getByText('Fix login redirect')).toBeInTheDocument()
  expect(screen.getByText('Add CSV export')).toBeInTheDocument()
  // The meter, and the summary line that stands in for it on phones.
  expect(screen.getAllByText(/2\/3 active/)).toHaveLength(2)
})

it('renders the Needs you zone before Pipeline, columns in the order received', async () => {
  renderWithProviders(<BoardPage />)
  await waitFor(() => expect(screen.getByTestId('column-parked')).toBeInTheDocument())
  const zones = screen.getAllByTestId(/^zone-/)
  expect(zones.map((z) => z.getAttribute('data-testid'))).toEqual(['zone-needs-you', 'zone-pipeline'])
  expect(within(zones[0]!).getByRole('heading', { name: 'Needs you' })).toBeInTheDocument()
  expect(within(zones[1]!).getByRole('heading', { name: 'Pipeline' })).toBeInTheDocument()
  const keysIn = (zone: HTMLElement) =>
    within(zone).getAllByTestId(/^column-/).map((el) => el.getAttribute('data-testid'))
  expect(keysIn(zones[0]!)).toEqual(['column-parked'])
  expect(keysIn(zones[1]!)).toEqual(['column-in-progress', 'column-awaiting-ci'])
})

it('a zone with no occupied columns leaves the row; its columns are chips', async () => {
  server.use(http.get('/api/board', () => HttpResponse.json({
    ...fx_board,
    columns: fx_board.columns.map((c) => (c.zone === 'needs-you' ? { ...c, cards: [] } : c)),
  })))
  renderWithProviders(<BoardPage />)
  // The snapshot paints Parked first; the live board then empties it.
  await screen.findByTestId('chip-parked')
  expect(screen.queryByTestId('column-parked')).not.toBeInTheDocument()
  expect(testIds(screen.getAllByTestId(/^zone-/))).toEqual(['zone-pipeline'])
})

it('the count strip is a list naming each empty column and its count', async () => {
  renderWithProviders(<BoardPage />)
  const strip = await screen.findByRole('list', { name: 'Empty columns' })
  expect(within(strip).getByRole('listitem', { name: 'Failed: 0' })).toBeInTheDocument()
  expect(within(strip).getByRole('listitem', { name: 'Wont do: 0' })).toBeInTheDocument()
  expect(within(strip).getAllByRole('listitem')).toHaveLength(8)
})

it('holds no column order of its own: a reordered board renders as received', async () => {
  const reversed = { ...fx_board, columns: [...fx_board.columns].reverse() }
  server.use(http.get('/api/board', () => HttpResponse.json(reversed)))
  renderWithProviders(<BoardPage />)
  await waitFor(() => expect(screen.getAllByTestId(/^column-/)[0]).toHaveAttribute('data-testid', 'column-awaiting-ci'))
  expect(testIds(screen.getAllByTestId(/^column-/)))
    .toEqual(['column-awaiting-ci', 'column-in-progress', 'column-parked'])
  expect(testIds(screen.getAllByTestId(/^chip-/))[0]).toBe('chip-wont-do')
  expect(screen.getAllByTestId(/^zone-/).map((z) => z.getAttribute('data-testid')))
    .toEqual(['zone-pipeline', 'zone-needs-you'])
})

it('renders a column\'s cards in the order the API returns (server sorts by score)', async () => {
  // The board API is the single source of card order (score-descending); the
  // page must render that order verbatim and never re-sort client-side.
  const parked = [
    { ...fx_board.columns.find((c) => c.key === 'parked')!.cards[0], issue: 42, score: 3.0 },
    { ...fx_board.columns.find((c) => c.key === 'parked')!.cards[1], issue: 45, score: 9.0 },
  ]
  const board = {
    ...fx_board,
    columns: fx_board.columns.map((c) =>
      c.key === 'parked' ? { ...c, cards: parked } : c,
    ),
  }
  server.use(http.get('/api/board', () => HttpResponse.json(board)))
  renderWithProviders(<BoardPage />)
  await waitFor(() => expect(screen.getByTestId('column-parked')).toBeInTheDocument())
  const order = within(screen.getByTestId('column-parked'))
    .getAllByTestId(/^card-/)
    .map((el) => el.getAttribute('data-testid'))
  expect(order).toEqual(['card-42', 'card-45'])
})

function fakeDataTransfer() {
  const data: Record<string, string> = {}
  return {
    data,
    setData: (k: string, v: string) => { data[k] = v },
    getData: (k: string) => data[k] ?? '',
    effectAllowed: '',
    dropEffect: '',
    types: [] as string[],
  }
}

it('dropping a card on the empty Wont do chip asks for confirmation before any intent fires', async () => {
  const posts: unknown[] = []
  server.use(
    http.post('/api/task/widget/42/cancel', async ({ request }) => {
      posts.push(await request.json())
      return HttpResponse.json(
        { status: 'pending', intent: '1-42-cancel.json' },
        { status: 202 },
      )
    }),
  )
  renderWithProviders(<BoardPage />)
  await waitFor(() => expect(screen.getByTestId('card-42')).toBeInTheDocument())
  const dt = fakeDataTransfer()
  fireEvent.dragStart(screen.getByTestId('card-42'), { dataTransfer: dt })
  fireEvent.dragOver(screen.getByTestId('chip-wont-do'), { dataTransfer: dt })
  fireEvent.drop(screen.getByTestId('chip-wont-do'), { dataTransfer: dt })
  // the double check: nothing fires until the operator confirms
  expect(posts).toEqual([])
  expect(screen.getByTestId('wont-do-confirm')).toHaveTextContent('#42')
  await userEvent.click(screen.getByRole('button', { name: "Confirm won't do?" }))
  await waitFor(() => expect(posts).toEqual([{}]))
  expect(screen.queryByTestId('wont-do-confirm')).not.toBeInTheDocument()
})

it('backing out of the drop confirmation fires nothing', async () => {
  const posts: unknown[] = []
  server.use(
    http.post('/api/task/widget/42/cancel', () => {
      posts.push('cancel')
      return HttpResponse.json(
        { status: 'pending', intent: '1-42-cancel.json' },
        { status: 202 },
      )
    }),
  )
  renderWithProviders(<BoardPage />)
  await waitFor(() => expect(screen.getByTestId('card-42')).toBeInTheDocument())
  const dt = fakeDataTransfer()
  fireEvent.dragStart(screen.getByTestId('card-42'), { dataTransfer: dt })
  fireEvent.drop(screen.getByTestId('chip-wont-do'), { dataTransfer: dt })
  await userEvent.click(screen.getByRole('button', { name: 'Keep task' }))
  expect(posts).toEqual([])
  expect(screen.queryByTestId('wont-do-confirm')).not.toBeInTheDocument()
})

it('AWKWARD: usage source unavailable shows the consequence, not a gauge', async () => {
  server.use(http.get('/api/usage', () => HttpResponse.json(usageUnavailable)))
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(
      screen.getByText('usage unknown — dispatcher will not spawn on anthropic'),
    ).toBeInTheDocument(),
  )
  expect(screen.queryByRole('progressbar', { name: /used$/ })).not.toBeInTheDocument()
})

it('AWKWARD: a pending intent renders a badge on the affected card', async () => {
  server.use(
    http.get('/api/pending-intents', () => HttpResponse.json(pendingReplyIntent)),
  )
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('pending-badge')).toBeInTheDocument(),
  )
  expect(screen.getByTestId('card-42')).toContainElement(
    screen.getByTestId('pending-badge'),
  )
  expect(screen.getByText('pending: reply')).toBeInTheDocument()
})

it('several pending intents on one issue all render — none silently dropped', async () => {
  server.use(
    http.get('/api/pending-intents', () =>
      HttpResponse.json({
        intents: [
          { action: 'reply', target: 'widget', issue: 42, actor: 'dev@localhost',
            created_at: '2026-07-25T11:58:00Z' },
          { action: 'kill', target: 'widget', issue: 42, actor: 'dev@localhost',
            created_at: '2026-07-25T11:59:00Z' },
        ],
      }),
    ),
  )
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getAllByTestId('pending-badge')).toHaveLength(2),
  )
  expect(screen.getByText('pending: reply')).toBeInTheDocument()
  expect(screen.getByText('pending: kill')).toBeInTheDocument()
})

it('a failing /api/usage states the gap instead of silently dropping the gauge', async () => {
  server.use(
    http.get('/api/usage', () =>
      HttpResponse.json({ detail: 'usage source exploded' }, { status: 500 }),
    ),
  )
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('usage-error')).toBeInTheDocument(),
  )
  expect(screen.getByTestId('usage-error')).toHaveTextContent(/^usage unknown — usage source exploded$/)
  expect(screen.queryByRole('progressbar', { name: /used$/ })).not.toBeInTheDocument()
})

it('two provider groups render side by side; the spawn chip shows once, on the gate provider', async () => {
  const twoProviders: UsageView = {
    providers: [
      {
        provider: 'anthropic', source: 'oauth',
        windows: [
          { kind: 'weekly', scope: 'Fable', used: 0.35, allowance: 0.289,
            headroom: -0.061, minutes_to_reset: 7320, severity: 'blocked' },
        ],
      },
      {
        provider: 'nvidia', source: 'ccusage',
        windows: [{ kind: 'session', scope: null, used: 0.4, allowance: 0.8,
          headroom: 0.4, minutes_to_reset: 45, severity: 'ok' }],
      },
    ],
    // Sonnet draws on no Fable window, so the gate is open despite it
    gate: { model: 'claude-sonnet-4-6', provider: 'anthropic', admitted: true,
      note: 'anthropic: no usage windows reported', minutes_to_reset: 0, binding: null },
  }
  server.use(http.get('/api/usage', () => HttpResponse.json(twoProviders)))
  renderWithProviders(<BoardPage />)
  // Both groups must render
  const anthropicGroup = await screen.findByText('anthropic')
  const nvidiaGroup = await screen.findByText('nvidia')
  // findByText returns the heading <span> whose direct text is the provider name;
  // .closest('div') reaches the per-provider group div that wraps it.
  const anthropicContainer = anthropicGroup.closest('div')!
  const nvidiaContainer = nvidiaGroup.closest('div')!
  expect(within(anthropicContainer).getByText('will spawn claude-sonnet-4-6')).toBeInTheDocument()
  expect(within(nvidiaContainer).queryByText(/^will (not )?spawn/)).not.toBeInTheDocument()
  // the chip is the default model's verdict, independent of any one window's severity
  expect(within(anthropicContainer).getByRole('progressbar')).toHaveAttribute(
    'aria-valuetext', expect.stringContaining('over the limit'),
  )
})

it('AWKWARD: two parked cards, only the login-parked one holds a unit', async () => {
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('card-45')).toBeInTheDocument(),
  )
  // login-parked: still running, still consuming
  expect(screen.getByTestId('card-45')).toHaveTextContent('holding a capacity unit')
  // CI-parked: container stopped, unit released
  expect(screen.getByTestId('card-46')).not.toHaveTextContent('holding a capacity unit')
})

it('the accented cards reconcile with the header meter', async () => {
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByRole('progressbar', { name: 'capacity units in use' }))
      .toBeInTheDocument(),
  )
  expect(screen.getAllByText('holding a capacity unit')).toHaveLength(2)
  expect(screen.getAllByTestId('cap-pip-filled')).toHaveLength(2)
  // The meter, and the summary line that stands in for it on phones.
  expect(screen.getAllByText(/2\/3 active/)).toHaveLength(2)
})

test('queued column renders ghost cards in rank order with count and stale hint', async () => {
  server.use(http.get('/api/board', () => HttpResponse.json({
    ...fx_board,
    upcoming: [
      { number: 73, target: 'widget', title: 'Ship dark mode', url: 'u', score: 8.5, boost: 0 },
      { number: 74, target: 'widget', title: 'Fix flaky test', url: 'u', score: 3.5, boost: 0 },
    ],
    upcoming_stale: true,
    next_claim: { ...fx_board.next_claim, verdict: 'will-claim', next_issue: 73, next_target: 'widget' },
  })))
  renderWithProviders(<BoardPage />)
  const queued = await screen.findByTestId('column-queued')
  const ghosts = within(queued).getAllByTestId(/^ghost-/)
  expect(ghosts.map((g) => g.dataset.testid)).toEqual(['ghost-73', 'ghost-74'])
  expect(within(queued).getByText('2')).toBeInTheDocument()  // header count
  expect(within(queued).getByText(/stale/)).toBeInTheDocument()
  expect(within(ghosts[0]!).getByText('next')).toBeInTheDocument()
})

test('same issue number on two targets renders two distinct ghosts', async () => {
  // Issue numbers are per-repo, so alpha#73 and beta#73 are different work.
  // The ghost key must include the target (duplicate React keys otherwise),
  // and only the forecast target may wear the "next" badge.
  server.use(http.get('/api/board', () => HttpResponse.json({
    ...fx_board,
    upcoming: [
      { number: 73, target: 'alpha', title: 'Alpha work', url: 'u', score: 8.5, boost: 0 },
      { number: 73, target: 'beta', title: 'Beta work', url: 'u', score: 3.5, boost: 0 },
    ],
    next_claim: { ...fx_board.next_claim, verdict: 'will-claim', next_issue: 73, next_target: 'beta' },
  })))
  renderWithProviders(<BoardPage />)
  const queued = await screen.findByTestId('column-queued')
  expect(within(queued).getByText('Alpha work')).toBeInTheDocument()
  expect(within(queued).getByText('Beta work')).toBeInTheDocument()
  // exactly one "next" badge — the alpha ghost must not claim the forecast
  expect(within(queued).getAllByText('next')).toHaveLength(1)
})

test('a failed queue action shows its error marker in the Queued header', async () => {
  server.use(
    http.get('/api/board', () => HttpResponse.json({
      ...fx_board,
      upcoming: [
        { number: 73, target: 'widget', title: 'Ship dark mode', url: 'u', score: 8.5, boost: 0 },
      ],
      upcoming_stale: true,
      next_claim: { ...fx_board.next_claim, verdict: 'no-candidates' },
    })),
    http.post('/api/queue/boost', () => HttpResponse.json({ detail: 'queue locked' }, { status: 422 })),
  )
  renderWithProviders(<BoardPage />)
  const queued = await screen.findByTestId('column-queued')
  // Ghosts arrive with /api/board, after the snapshot paints.
  await userEvent.click(await within(queued).findByRole('button', { name: 'Details for widget#73' }))
  await userEvent.click(within(queued).getByRole('button', { name: 'Boost' }))
  // Collapsing the ghost again must not hide degraded queue state.
  await userEvent.click(within(queued).getByRole('button', { name: 'Details for widget#73' }))
  // Visible text, not a tooltip: touch has no hover.
  expect(await within(queued).findByTestId('queue-error')).toHaveTextContent('queue locked')
  expect(within(queued).getByTestId('queue-stale')).toBeInTheDocument()
})

test('a Queued column holding only ghosts stays in the row', async () => {
  server.use(http.get('/api/board', () => HttpResponse.json({
    ...fx_board,
    upcoming: [
      { number: 73, target: 'widget', title: 'Ship dark mode', url: 'u', score: 8.5, boost: 0 },
    ],
  })))
  renderWithProviders(<BoardPage />)
  const queued = await screen.findByTestId('column-queued')
  expect(within(queued).getByTestId('ghost-73')).toBeInTheDocument()
  expect(screen.queryByTestId('chip-queued')).not.toBeInTheDocument()
})

test('an empty Queued chip still carries the stale marker', async () => {
  server.use(http.get('/api/board', () => HttpResponse.json({ ...fx_board, upcoming_stale: true })))
  renderWithProviders(<BoardPage />)
  const chip = await screen.findByTestId('chip-queued')
  expect(await within(chip).findByTestId('queue-stale')).toBeInTheDocument()
  expect(screen.queryByTestId('column-queued')).not.toBeInTheDocument()
})

it('a task card links to /task/{target}/{issue}', async () => {
  renderWithProviders(<BoardPage />)
  await waitFor(() => expect(screen.getByTestId('card-41')).toBeInTheDocument())
  expect(within(screen.getByTestId('card-41')).getByRole('link')).toHaveAttribute(
    'href', '/task/widget/41',
  )
})

it('two claimed cards sharing an issue number across targets both render with distinct keys', async () => {
  // Issue numbers are per-target: alpha#73 and beta#73 are different work.
  // BoardColumn's card key must include the target, or React would collapse
  // one card into the other (silently dropping it, not just warning).
  const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
  const cardA = { ...inProgressCard, target: 'alpha', issue: 73, title: 'Alpha claimed work' }
  const cardB = { ...inProgressCard, target: 'beta', issue: 73, title: 'Beta claimed work' }
  server.use(http.get('/api/board', () => HttpResponse.json({
    ...fx_board,
    columns: fx_board.columns.map((c) =>
      c.key === 'in-progress' ? { ...c, cards: [cardA, cardB] } : c,
    ),
  })))
  renderWithProviders(<BoardPage />)
  await waitFor(() => expect(screen.getByText('Alpha claimed work')).toBeInTheDocument())
  expect(screen.getByText('Beta claimed work')).toBeInTheDocument()
  const keyWarning = errorSpy.mock.calls.some((args) =>
    String(args[0]).includes('same key'),
  )
  expect(keyWarning).toBe(false)
  errorSpy.mockRestore()
})

it('a legacy target-less pending intent still badges the card by issue', async () => {
  server.use(
    http.get('/api/pending-intents', () => HttpResponse.json({
      intents: [{ action: 'reply', target: '', issue: 42, actor: 'dev@localhost',
                  created_at: '2026-07-25T11:58:00Z' }],
    })),
  )
  renderWithProviders(<BoardPage />)
  await waitFor(() =>
    expect(screen.getByTestId('pending-badge')).toBeInTheDocument(),
  )
  expect(screen.getByTestId('card-42')).toContainElement(
    screen.getByTestId('pending-badge'),
  )
})

test('header shows next-claim line and median cycle', async () => {
  server.use(http.get('/api/board', () => HttpResponse.json({
    ...fx_board,
    median_cycle_seconds: 7200,
    next_claim: { verdict: 'capacity-full', next_pass_eta: new Date(Date.now() + 300_000).toISOString(), next_issue: 0, next_target: '', minutes_to_reset: 0 },
  })))
  renderWithProviders(<BoardPage />)
  expect(await screen.findByTestId('next-claim')).toHaveTextContent(/capacity full/i)
  expect(screen.getByText(/≈2h per task/)).toBeInTheDocument()
})


it('renders saved cards while live checks wait, then adds the ranked queue', async () => {
  let finish!: () => void
  const pending = new Promise<void>((resolve) => { finish = resolve })
  server.use(http.get('/api/board', async () => {
    await pending
    return HttpResponse.json(fx_board)
  }))
  renderWithProviders(<BoardPage />)
  try {
    expect(await screen.findByText('Fix login redirect')).toBeInTheDocument()
    expect(screen.getByText('loading queue and forecast…')).toBeInTheDocument()
    // Ghosts only arrive with the live board, so Queued is a chip until then.
    expect(screen.getByTestId('chip-queued')).toBeInTheDocument()
    expect(screen.getAllByTestId(/^(column|chip)-/)).toHaveLength(11)
  } finally {
    finish()
  }
  await waitFor(() => expect(screen.queryByText('loading queue and forecast…')).not.toBeInTheDocument())
  expect(screen.getByText('Fix login redirect')).toBeInTheDocument()
})

it('keeps saved cards visible when live checks fail', async () => {
  server.use(http.get('/api/board', () => HttpResponse.json(
    { detail: 'live checks failed' }, { status: 503 },
  )))
  renderWithProviders(<BoardPage />)
  expect(await screen.findByText('Fix login redirect')).toBeInTheDocument()
  expect(await screen.findByText('queue and forecast unavailable')).toBeInTheDocument()
})

it('falls back to the full board if the snapshot fails', async () => {
  server.use(http.get('/api/board/snapshot', () => HttpResponse.json(
    { detail: 'snapshot unavailable' }, { status: 503 },
  )))
  renderWithProviders(<BoardPage />)
  expect(await screen.findByText('Fix login redirect')).toBeInTheDocument()
})

describe('phone tabs', () => {
  const tabNames = () => screen.getAllByRole('tab').map((t) => t.textContent)
  const selected = () => screen.getByRole('tab', { selected: true }).textContent

  it('one tab per occupied column, in the order received, with counts', async () => {
    renderWithProviders(<BoardPage />)
    const tablist = await screen.findByRole('tablist', { name: 'Columns' })
    expect(within(tablist).getAllByRole('tab').map((t) => t.textContent))
      .toEqual(['Parked 2', 'In progress 1', 'Awaiting CI 1'])
    expect(screen.getByRole('tab', { name: 'Parked 2' })).toHaveAttribute('aria-controls', 'column-parked')
    expect(document.getElementById('column-parked')).toBe(screen.getByTestId('column-parked'))
    expect(screen.getByRole('tabpanel', { name: 'Parked 2' })).toBe(screen.getByTestId('column-parked'))
  })

  it('with no parameter the first occupied column is active', async () => {
    renderWithProviders(<BoardPage />)
    await screen.findByRole('tablist')
    expect(selected()).toBe('Parked 2')
  })

  it('the column parameter picks the tab; an empty or unknown key falls back', async () => {
    const { unmount } = renderWithProviders(<BoardPage />, { route: '/?column=awaiting-ci' })
    await screen.findByRole('tablist')
    expect(selected()).toBe('Awaiting CI 1')
    unmount()
    renderWithProviders(<BoardPage />, { route: '/?column=failed' })
    await screen.findByRole('tablist')
    expect(selected()).toBe('Parked 2')
  })

  it('selecting a tab selects it; arrow keys move along the row', async () => {
    renderWithProviders(<BoardPage />)
    await screen.findByRole('tablist')
    await userEvent.click(screen.getByRole('tab', { name: 'In progress 1' }))
    expect(selected()).toBe('In progress 1')
    expect(screen.getByRole('tab', { name: 'In progress 1' })).toHaveAttribute('tabindex', '0')
    expect(screen.getByRole('tab', { name: 'Parked 2' })).toHaveAttribute('tabindex', '-1')
    await userEvent.keyboard('{ArrowRight}')
    expect(selected()).toBe('Awaiting CI 1')
    expect(screen.getByRole('tab', { name: 'Awaiting CI 1' })).toHaveFocus()
    await userEvent.keyboard('{ArrowRight}')
    expect(selected()).toBe('Parked 2')
    await userEvent.keyboard('{End}')
    expect(selected()).toBe('Awaiting CI 1')
    expect(tabNames()).toHaveLength(3)
  })

  it('the default tab stays put when Queued fills in front of it', async () => {
    const columns = fx_board.columns.map((c) => (c.zone === 'needs-you' ? { ...c, cards: [] } : c))
    let release = () => {}
    const boardHeld = new Promise<void>((resolve) => { release = resolve })
    server.use(
      http.get('/api/board/snapshot', () => HttpResponse.json({
        columns, capacity: fx_board.capacity, median_cycle_seconds: fx_board.median_cycle_seconds,
      })),
      http.get('/api/board', async () => {
        await boardHeld
        return HttpResponse.json({
          ...fx_board, columns,
          upcoming: [{ number: 73, target: 'widget', title: 'Ship dark mode', url: '', score: 1, boost: 0 }],
        })
      }),
    )
    renderWithProviders(<BoardPage />)
    await screen.findByRole('tablist')
    expect(selected()).toBe('In progress 1')
    release()
    expect(await screen.findByRole('tab', { name: 'Queued 1' })).toBeInTheDocument()
    expect(selected()).toBe('In progress 1')
  })

  it('a drained column hands the URL to the fallback, so a refill does not jump back', async () => {
    const drained = fx_board.columns.map((c) => (c.key === 'awaiting-ci' ? { ...c, cards: [] } : c))
    let current = drained
    server.use(
      http.get('/api/board/snapshot', () => HttpResponse.json({
        columns: current, capacity: fx_board.capacity, median_cycle_seconds: fx_board.median_cycle_seconds,
      })),
      http.get('/api/board', () => HttpResponse.json({ ...fx_board, columns: current })),
    )
    const { queryClient } = renderWithProviders(<BoardPage />, { route: '/?column=awaiting-ci' })
    await screen.findByRole('tablist')
    expect(selected()).toBe('Parked 2')
    current = fx_board.columns
    await queryClient.invalidateQueries()
    expect(await screen.findByRole('tab', { name: 'Awaiting CI 1' })).toBeInTheDocument()
    expect(selected()).toBe('Parked 2')
  })

  it('a link to Queued survives the ghost-less snapshot', async () => {
    let release = () => {}
    const boardHeld = new Promise<void>((resolve) => { release = resolve })
    server.use(http.get('/api/board', async () => {
      await boardHeld
      return HttpResponse.json({
        ...fx_board,
        upcoming: [{ number: 73, target: 'widget', title: 'Ship dark mode', url: '', score: 1, boost: 0 }],
      })
    }))
    renderWithProviders(<BoardPage />, { route: '/?column=queued' })
    await screen.findByRole('tablist')
    expect(selected()).toBe('Parked 2')
    release()
    await waitFor(() => expect(selected()).toBe('Queued 1'))
  })

  it('Queued counts its ghosts', async () => {
    server.use(http.get('/api/board', () => HttpResponse.json({
      ...fx_board,
      upcoming: [{ number: 73, target: 'widget', title: 'Ship dark mode', url: '', score: 1, boost: 0 }],
    })))
    renderWithProviders(<BoardPage />)
    expect(await screen.findByRole('tab', { name: 'Queued 1' })).toBeInTheDocument()
    expect(tabNames()).toEqual(['Parked 2', 'Queued 1', 'In progress 1', 'Awaiting CI 1'])
  })
})

test('phone header: one summary line with capacity and the next-claim verdict', async () => {
  server.use(http.get('/api/board', () => HttpResponse.json({
    ...fx_board,
    next_claim: { verdict: 'capacity-full', next_pass_eta: new Date(Date.now() + 300_000).toISOString(), next_issue: 0, next_target: '', minutes_to_reset: 0, blocked_by: '' },
  })))
  renderWithProviders(<BoardPage />)
  const summary = await screen.findByRole('button', { name: /2\/3 active capacity full — waits for a free slot/ })
  expect(summary).toHaveAttribute('aria-expanded', 'false')
  const row = document.getElementById(summary.getAttribute('aria-controls')!)!
  expect(row).toContainElement(screen.getByTestId('next-claim'))
  await userEvent.click(summary)
  expect(summary).toHaveAttribute('aria-expanded', 'true')
})
