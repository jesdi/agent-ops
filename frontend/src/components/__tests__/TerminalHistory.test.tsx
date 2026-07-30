import { fireEvent, screen, waitFor } from '@testing-library/react'
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

function makeScrollable(el: HTMLElement) {
  Object.defineProperty(el, 'scrollHeight', { value: 1000, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: 200, configurable: true })
}

it('scrolling back to the bottom returns to live', async () => {
  const onClose = vi.fn()
  renderWithProviders(<TerminalHistory issue={42} onClose={onClose} />)
  const pane = screen.getByTestId('terminal-history-pane')
  makeScrollable(pane)

  // scroll up into history (500 px above the bottom) — arms the auto-return
  pane.scrollTop = 300
  fireEvent.scroll(pane)
  expect(onClose).not.toHaveBeenCalled()

  // back down to within the 4 px threshold of the bottom
  pane.scrollTop = 796
  fireEvent.scroll(pane)
  expect(onClose).toHaveBeenCalledTimes(1)
})

it('does not close from scroll events while still at the bottom', async () => {
  const onClose = vi.fn()
  renderWithProviders(<TerminalHistory issue={42} onClose={onClose} />)
  const pane = screen.getByTestId('terminal-history-pane')
  makeScrollable(pane)

  // the pane opens AT the bottom; the open-scrolled-to-bottom effect and any
  // bounce events there must not close the overlay the user just opened
  pane.scrollTop = 800 // exactly at bottom: 1000 - 800 - 200 = 0
  fireEvent.scroll(pane)
  fireEvent.scroll(pane)
  expect(onClose).not.toHaveBeenCalled()
})

it('stays open while scrolled up reading history', async () => {
  const onClose = vi.fn()
  renderWithProviders(<TerminalHistory issue={42} onClose={onClose} />)
  const pane = screen.getByTestId('terminal-history-pane')
  makeScrollable(pane)

  pane.scrollTop = 300
  fireEvent.scroll(pane)
  pane.scrollTop = 100
  fireEvent.scroll(pane)
  expect(onClose).not.toHaveBeenCalled()
})

it('refetch while reading history does not close the overlay', async () => {
  const onClose = vi.fn()
  const { queryClient } = renderWithProviders(
    <TerminalHistory issue={42} onClose={onClose} />,
  )
  const pane = screen.getByTestId('terminal-history-pane')
  makeScrollable(pane)

  // wait for initial content to load
  await waitFor(() =>
    expect(pane.textContent).toContain('last line'),
  )

  // scroll up to arm the auto-return
  pane.scrollTop = 300
  fireEvent.scroll(pane)
  expect(onClose).not.toHaveBeenCalled()

  // simulate new session output: change the MSW handler and refetch
  server.use(
    http.get('/api/task/42/history', () =>
      HttpResponse.json({
        text: 'first line\nmore output\neven more\nlast line',
      }),
    ),
  )
  const { queryKeys } = await import('../../hooks/queryKeys')
  await queryClient.invalidateQueries({ queryKey: queryKeys.taskHistory(42) })

  // wait for new content to load
  await waitFor(() =>
    expect(pane.textContent).toContain('even more'),
  )

  // the effect runs programmatic scroll to bottom, which in a real browser
  // dispatches a scroll event. jsdom doesn't auto-dispatch, so we simulate it.
  // WITHOUT the disarm, armedRef would still be true, so this would call onClose().
  pane.scrollTop = 800 // effect set this to scrollHeight
  fireEvent.scroll(pane)
  expect(onClose).not.toHaveBeenCalled()

  // verify auto-return is not permanently disabled: scroll up and back down
  pane.scrollTop = 300
  fireEvent.scroll(pane)
  expect(onClose).not.toHaveBeenCalled()

  pane.scrollTop = 800
  fireEvent.scroll(pane)
  expect(onClose).toHaveBeenCalledTimes(1)
})
