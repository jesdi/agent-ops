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
// track. A task's pin is always its own track: no ticket has a track of its
// own (specs/pinned-tracks, requirement 7).

it('a card pinned to its own track shows the pin once, not "track" as well', async () => {
  renderWithProviders(<TaskCardView card={pinnedCard} />)
  await userEvent.click(screen.getByRole('button', { name: `Details for ${pinnedCard.target}#${pinnedCard.issue}` }))
  expect(screen.getByText('pinned to frontend')).toBeInTheDocument()
  expect(screen.queryByText(/track frontend/)).toBeNull()
})

it('the task page pinned to its own track shows the pin once', async () => {
  server.use(
    http.get('/api/task/:target/:issue', () => HttpResponse.json({ ...taskDetail, card: pinnedCard })),
    ...defaultHandlers)
  renderWithProviders(
    <Routes><Route path="/task/:target/:issue" element={<TaskPage />} /></Routes>,
    { route: `/task/${pinnedCard.target}/${pinnedCard.issue}` })
  const header = (await screen.findByText(/pinned to frontend/)).parentElement!
  expect(header).toHaveTextContent(/claude-fable-5-1 · pinned to frontend · branch/)
})
