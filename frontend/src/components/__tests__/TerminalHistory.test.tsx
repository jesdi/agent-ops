import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'
import { TerminalHistory } from '../TerminalHistory'

beforeEach(() => {
  server.use(
    http.get('/api/task/42/history', () =>
      HttpResponse.json({ text: 'first line\nlast line' }),
    ),
  )
})

it('renders fetched history text', async () => {
  renderWithProviders(<TerminalHistory issue={42} onClose={() => {}} />)
  await waitFor(() =>
    expect(screen.getByTestId('terminal-history-pane').textContent).toContain(
      'last line',
    ),
  )
})

it('the back control returns to live', async () => {
  const onClose = vi.fn()
  renderWithProviders(<TerminalHistory issue={42} onClose={onClose} />)
  await userEvent.click(
    screen.getByRole('button', { name: 'Return to live terminal' }),
  )
  expect(onClose).toHaveBeenCalledTimes(1)
})

it('Escape returns to live', async () => {
  const onClose = vi.fn()
  renderWithProviders(<TerminalHistory issue={42} onClose={onClose} />)
  await userEvent.keyboard('{Escape}')
  expect(onClose).toHaveBeenCalledTimes(1)
})
