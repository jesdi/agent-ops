import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { taskDetail } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { TaskPage } from '../TaskPage'

// `implement_progress` is not in the generated API types yet: cast, so only
// these tests go red.
function renderWith(progress: string | null) {
  server.use(...defaultHandlers,
    http.get('/api/task/:target/:issue', () =>
      HttpResponse.json({ ...taskDetail, implement_progress: progress })))
  return renderWithProviders(
    <Routes><Route path="/task/:target/:issue" element={<TaskPage />} /></Routes>,
    { route: '/task/widget/42' })
}

it('shows the implement progress the session reported', async () => {
  renderWith('2/4 tickets merged')
  const line = await screen.findByTestId('implement-progress')
  expect(line).toHaveTextContent('2/4 tickets merged')
})

it('shows no progress line and no placeholder when there is none', async () => {
  // Not vacuous: a line shows first, then goes away when the report is null.
  const { queryClient } = renderWith('2/4 tickets merged')
  await screen.findByTestId('implement-progress')
  server.use(http.get('/api/task/:target/:issue', () =>
    HttpResponse.json({ ...taskDetail, implement_progress: null })))
  await queryClient.invalidateQueries()
  await waitFor(() =>
    expect(screen.queryByTestId('implement-progress')).toBeNull())
  expect(screen.queryByText(/tickets merged|no progress|progress/i)).toBeNull()
})
