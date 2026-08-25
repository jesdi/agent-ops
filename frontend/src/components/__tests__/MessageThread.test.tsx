import { render, screen, within } from '@testing-library/react'
import { MessageThread } from '../MessageThread'
import type { MessageView } from '../../lib/api'

const msg = (over: Partial<MessageView>): MessageView => ({
  id: 'm1', text: 'use oauth', actor: 'jesdi@github',
  created_at: '2026-08-12T10:00:00Z', delivered_at: '', state: 'queued',
  ...over,
})

test('renders one row per message with its state chip', () => {
  render(<MessageThread messages={[
    msg({ id: 'a', text: 'delivered one', state: 'delivered',
          delivered_at: '2026-08-12T10:05:00Z' }),
    msg({ id: 'b', text: 'queued one', state: 'queued' }),
    msg({ id: 'c', text: 'just sent', state: 'sending' }),
  ]} />)
  for (const [id, text, chip] of [
    ['a', 'delivered one', 'delivered'],
    ['b', 'queued one', 'queued'],
    ['c', 'just sent', 'sending'],
  ] as [string, string, string][]) {
    const row = screen.getByTestId(`message-${id}`)
    expect(within(row).getByText(text)).toBeInTheDocument()
    expect(within(row).getByTestId('message-state')).toHaveTextContent(chip)
  }
})

test('renders nothing when there are no messages', () => {
  const { container } = render(<MessageThread messages={[]} />)
  expect(container).toBeEmptyDOMElement()
})

test('a delivered message shows WHEN it was delivered, not just that it was', () => {
  // relativeTime(delivered_at) renders "just now" | "42s ago" | "5m ago" | …,
  // so the chip must read "delivered <something> ago" (or "delivered just
  // now") — asserting the bare word would still pass with the timestamp gone.
  const fiveMinutesAgo = new Date(Date.now() - 5 * 60_000).toISOString()
  render(<MessageThread messages={[
    msg({ id: 'a', state: 'delivered', delivered_at: fiveMinutesAgo }),
  ]} />)
  const chip = within(screen.getByTestId('message-a')).getByTestId('message-state')
  expect(chip).toHaveTextContent(/^delivered \d+m ago$/)
})
