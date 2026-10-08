import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { TaskCardView } from '../TaskCard'
import { TaskPage } from '../../pages/TaskPage'
import { defaultHandlers } from '../../test/handlers'
import { pinnedCard, taskDetail } from '../../test/fixtures'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'

// Ticket 03, review: the track text is not repeated when it is the pinned
// track; a ticket track (06) that differs from the task track shows both.

const samePin = { ...pinnedCard, track: 'frontend', pinned_track: 'frontend' }

it('a card pinned to its own track shows the pin once, not "track" as well', async () => {
  renderWithProviders(<TaskCardView card={samePin} />)
  await userEvent.click(screen.getByRole('button', { name: `Details for ${samePin.target}#${samePin.issue}` }))
  expect(screen.getByText('pinned to frontend')).toBeInTheDocument()
  expect(screen.queryByText(/track frontend/)).toBeNull()
})

it('a card pinned to another track than its own shows both', async () => {
  renderWithProviders(<TaskCardView card={pinnedCard} />)
  await userEvent.click(screen.getByRole('button', { name: `Details for ${pinnedCard.target}#${pinnedCard.issue}` }))
  expect(screen.getByText('pinned to frontend')).toBeInTheDocument()
  expect(screen.getByText('track architecture')).toBeInTheDocument()
})

function renderTask(card: typeof samePin) {
  server.use(
    http.get('/api/task/:target/:issue', () => HttpResponse.json({ ...taskDetail, card })),
    ...defaultHandlers)
  return renderWithProviders(
    <Routes><Route path="/task/:target/:issue" element={<TaskPage />} /></Routes>,
    { route: `/task/${card.target}/${card.issue}` })
}

it('the task page pinned to its own track shows the pin once', async () => {
  renderTask(samePin)
  const header = (await screen.findByText(/pinned to frontend/)).parentElement!
  expect(header).toHaveTextContent(/claude-fable-5-1 · pinned to frontend · branch/)
})

it('the task page pinned to another track shows both', async () => {
  renderTask(pinnedCard)
  const header = (await screen.findByText(/pinned to frontend/)).parentElement!
  expect(header).toHaveTextContent(/pinned to frontend · track architecture · branch/)
})
