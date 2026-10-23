import { screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { taskDetail } from '../../test/fixtures'
import { renderWithProviders } from '../../test/render'
import { TaskPage } from '../TaskPage'

const pr = (number: number, extra: object) => ({
  repo: 'o/back', number, url: `https://github.com/o/back/pull/${number}`, state: 'open',
  outcome: '', checks: 'passing', ci_fix_attempts: 0, feedback_rounds: 0,
  depends_on: [], dependency: 'satisfied', waiting_on: [], ...extra,
})

function renderWith(required_prs: object[], dependency_errors: string[] = []) {
  server.use(
    http.get('/api/task/:target/:issue', () =>
      HttpResponse.json({ ...taskDetail, required_prs, dependency_errors })),
    ...defaultHandlers)
  renderWithProviders(
    <Routes><Route path="/task/:target/:issue" element={<TaskPage />} /></Routes>,
    { route: '/task/widget/42' })
}

it('shows a dependent PR waiting for its unmerged prerequisite', async () => {
  renderWith([pr(7, {}), pr(9, { dependency: 'waiting', waiting_on: ['o/back#7'] })])
  expect(await screen.findByTestId('pr-waiting')).toHaveTextContent('waiting for o/back#7 to merge')
})

it('shows invalid edges and their validation errors', async () => {
  renderWith([pr(9, { dependency: 'invalid' })], ['o/back#9 depends on itself'])
  expect(await screen.findByTestId('pr-invalid')).toBeInTheDocument()
  expect(screen.getByTestId('dependency-error')).toHaveTextContent('depends on itself')
})
