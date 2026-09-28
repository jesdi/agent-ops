// Locked, black-box acceptance test for ticket 08: stage sessions run on
// Codex. See .agent/tickets/08-codex-stage-sessions.md and
// docs/specs/2026-09-24-codex-runtime-design.md (Console): "The claude.ai
// Remote Control link-out is hidden for a session whose pick is not
// anthropic."
import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { taskDetail } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { TaskPage } from '../TaskPage'

function renderTask() {
  return renderWithProviders(
    <Routes>
      <Route path="/task/:target/:issue" element={<TaskPage />} />
    </Routes>,
    { route: '/task/widget/42' },
  )
}

beforeEach(() => {
  server.use(...defaultHandlers)
})

it('shows the claude.ai Remote Control link-out for an anthropic pick', async () => {
  renderTask()
  await waitFor(() => expect(screen.getByText('Fix login redirect')).toBeInTheDocument())
  expect(screen.getByRole('link', { name: /Open in Claude/ })).toBeInTheDocument()
})

it('hides the claude.ai Remote Control link-out for a session whose pick is not anthropic', async () => {
  server.use(
    http.get('/api/task/:target/:issue', () =>
      HttpResponse.json({
        ...taskDetail,
        card: { ...taskDetail.card, model: 'openai/gpt-5-codex' },
      }),
    ),
  )
  renderTask()
  await waitFor(() => expect(screen.getByText('Fix login redirect')).toBeInTheDocument())
  expect(screen.queryByRole('link', { name: /Open in Claude/ })).not.toBeInTheDocument()
})
