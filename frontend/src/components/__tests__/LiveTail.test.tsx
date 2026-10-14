import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { LiveTail } from '../LiveTail'

function makeScrollable(el: HTMLElement) {
  Object.defineProperty(el, 'scrollHeight', { value: 1000, configurable: true })
  Object.defineProperty(el, 'clientHeight', { value: 200, configurable: true })
}

it('follows new output at the bottom, holds while scrolled up, and jumps back on demand', async () => {
  const { rerender } = render(<LiveTail text="a" />)
  const pane = screen.getByTestId('pane-tail')
  makeScrollable(pane)

  rerender(<LiveTail text="a\nb" />)
  expect(pane.scrollTop).toBe(1000)
  expect(screen.queryByRole('button', { name: 'Scroll to bottom' })).toBeNull()

  pane.scrollTop = 300
  fireEvent.scroll(pane)
  rerender(<LiveTail text="a\nb\nc" />)
  expect(pane.scrollTop).toBe(300)

  await userEvent.click(screen.getByRole('button', { name: 'Scroll to bottom' }))
  expect(pane.scrollTop).toBe(1000)
})
