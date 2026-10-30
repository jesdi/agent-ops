import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { useLocation } from 'react-router'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { board as fx_board, inProgressCard } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import type { BoardView, GhostCard, ProjectsView } from '../../lib/api'
import { BoardPage } from '../BoardPage'

// widget (the fixtures' project) plus a paused project, side, and an empty
// one, spare. side has a card and a ghost; a card from a repository that left
// the config has no project.
const projects: ProjectsView = {
  projects: [
    { name: 'widget', paused: false, repositories: ['widget'] },
    { name: 'side', paused: true, repositories: ['gadget', 'gizmo'] },
    { name: 'spare', paused: false, repositories: ['spare'] },
  ],
}
const sideCard = { ...inProgressCard, issue: 7, repository: 'gadget', project: 'side', title: 'Side work' }
const orphanCard = { ...inProgressCard, issue: 8, repository: 'gone', project: '', title: 'Orphan work', column: 'done' }
const sideGhost: GhostCard = {
  number: 73, repository: 'gadget', project: 'side', title: 'Side candidate',
  url: '', score: 1, boost: 0,
}

function boardWith(extra: { cards?: Record<string, typeof inProgressCard[]>; ghosts?: GhostCard[] }): BoardView {
  return {
    ...fx_board,
    columns: fx_board.columns.map((c) => ({
      ...c,
      cards: [...c.cards, ...(extra.cards?.[c.key] ?? [])],
      ghosts: c.key === 'queued' ? (extra.ghosts ?? []) : c.ghosts,
    })),
  }
}

function serve(board: BoardView) {
  server.use(
    http.get('/api/projects', () => HttpResponse.json(projects)),
    http.get('/api/board', () => HttpResponse.json(board)),
  )
}

function Search() {
  return <output data-testid="search">{useLocation().search}</output>
}

beforeEach(() => server.use(...defaultHandlers))

const projectTabs = () => within(screen.getByRole('tablist', { name: 'Projects' })).getAllByRole('tab')

it('shows one tab per project in API order, paused ones badged, the first selected', async () => {
  serve(boardWith({ cards: { 'in-progress': [sideCard] } }))
  renderWithProviders(<BoardPage />)
  await screen.findByText('Add CSV export')
  expect(projectTabs().map((t) => t.textContent)).toEqual(['widget', 'sidePaused', 'spare'])
  expect(projectTabs()[0]).toHaveAttribute('aria-selected', 'true')
  // The widget tab filters side's card out, and the column counts follow.
  expect(screen.queryByText('Side work')).not.toBeInTheDocument()
  expect(within(screen.getByTestId('column-in-progress')).getByText('1')).toBeInTheDocument()
})

it('a tab click selects the project in the URL and shows only its work', async () => {
  serve(boardWith({ cards: { 'in-progress': [sideCard] } }))
  renderWithProviders(<><BoardPage /><Search /></>)
  await screen.findByText('Add CSV export')
  await userEvent.click(screen.getByRole('tab', { name: /side/ }))
  expect(screen.getByTestId('search')).toHaveTextContent('?project=side')
  expect(screen.getByText('Side work')).toBeInTheDocument()
  expect(screen.queryByText('Add CSV export')).not.toBeInTheDocument()
  expect(screen.queryByTestId('column-parked')).not.toBeInTheDocument()
})

it('an unknown project in the URL falls back to the first tab', async () => {
  serve(boardWith({}))
  renderWithProviders(<BoardPage />, { route: '/?project=nope' })
  await screen.findByText('Add CSV export')
  expect(projectTabs()[0]).toHaveAttribute('aria-selected', 'true')
})

it('a paused project says new issues are not claimed and marks its ghosts, whose actions still work', async () => {
  serve(boardWith({ ghosts: [sideGhost] }))
  let boosted: unknown = null
  server.use(http.post('/api/queue/boost', async ({ request }) => {
    boosted = await request.json()
    return HttpResponse.json({ ok: true, reason: '' })
  }))
  renderWithProviders(<BoardPage />, { route: '/?project=side' })
  expect(await screen.findByText(/new issues are not claimed/)).toBeInTheDocument()
  const ghost = await screen.findByTestId('ghost-73')
  expect(within(ghost).getByText('paused')).toBeInTheDocument()
  await userEvent.click(within(ghost).getByRole('button', { name: /Details for/ }))
  await userEvent.click(within(ghost).getByRole('button', { name: 'Boost' }))
  await waitFor(() => expect(boosted).toEqual({ issue: 73, amount: 1 }))
})

it('an enabled project shows no paused notice', async () => {
  serve(boardWith({}))
  renderWithProviders(<BoardPage />)
  await screen.findByText('Add CSV export')
  expect(screen.queryByText(/new issues are not claimed/)).not.toBeInTheDocument()
})

it('a project with no work renders every column empty', async () => {
  serve(boardWith({}))
  renderWithProviders(<BoardPage />, { route: '/?project=spare' })
  const strip = await screen.findByRole('list', { name: 'Empty columns' })
  expect(within(strip).getAllByRole('listitem')).toHaveLength(fx_board.columns.length)
  expect(screen.queryAllByTestId(/^column-/)).toEqual([])
})

it('cards whose repository left the config show under a trailing Other tab', async () => {
  serve(boardWith({ cards: { done: [orphanCard] } }))
  renderWithProviders(<BoardPage />)
  await screen.findByText('Add CSV export')
  expect(projectTabs().map((t) => t.textContent)).toEqual(['widget', 'sidePaused', 'spare', 'Other'])
  expect(screen.queryByText('Orphan work')).not.toBeInTheDocument()
  await userEvent.click(screen.getByRole('tab', { name: 'Other' }))
  expect(screen.getByText('Orphan work')).toBeInTheDocument()
})

it('no Other tab while every card has a project', async () => {
  serve(boardWith({}))
  renderWithProviders(<BoardPage />)
  await screen.findByText('Add CSV export')
  expect(screen.queryByRole('tab', { name: 'Other' })).not.toBeInTheDocument()
})

it('arrow keys move the project selection and focus, wrapping at the ends', async () => {
  serve(boardWith({}))
  renderWithProviders(<BoardPage />)
  await screen.findByText('Add CSV export')
  projectTabs()[0]!.focus()
  await userEvent.keyboard('{ArrowLeft}')
  expect(screen.getByRole('tab', { name: 'spare' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('tab', { name: 'spare' })).toHaveFocus()
  await userEvent.keyboard('{Home}')
  expect(projectTabs()[0]).toHaveFocus()
})

it('without the project list the board shows every card, unfiltered', async () => {
  serve(boardWith({ cards: { 'in-progress': [sideCard] } }))
  server.use(http.get('/api/projects', () => HttpResponse.json({ detail: 'boom' }, { status: 500 })))
  renderWithProviders(<BoardPage />)
  expect(await screen.findByText('Side work')).toBeInTheDocument()
  expect(screen.getByText('Add CSV export')).toBeInTheDocument()
  expect(screen.queryByRole('tablist', { name: 'Projects' })).not.toBeInTheDocument()
  expect(screen.queryByRole('tabpanel')).not.toBeInTheDocument()
})
