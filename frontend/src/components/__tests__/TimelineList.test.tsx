import { screen } from '@testing-library/react'
import { renderWithProviders } from '../../test/render'
import { TimelineList } from '../TimelineList'
import type { EventEntry } from '../../lib/api'

const scoped: EventEntry = {
  ts: '2026-07-25T11:30:00Z', event: 'stage-started', repository: 'widget',
  issue: 41, stage: 'implement', model: 'opus', actor: 'dispatcher', detail: '',
}

const unscoped: EventEntry = {
  ts: '2026-07-25T10:00:00Z', event: 'intent-applied', repository: '',
  issue: 42, stage: '', model: '', actor: 'operator', detail: 'reply',
}

test('links a scoped event row to its task view', () => {
  renderWithProviders(<TimelineList events={[scoped]} />)
  expect(screen.getByRole('link')).toHaveAttribute('href', '/task/widget/41')
})

test('a repository-less event row renders without a broken /task//N href', () => {
  renderWithProviders(<TimelineList events={[unscoped]} />)
  const row = screen.getByTestId('timeline-row')
  expect(row).toHaveTextContent('intent-applied')
  expect(row).toHaveTextContent('#42')
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
  const anyBrokenHref = document.querySelector('a[href="/task//42"]')
  expect(anyBrokenHref).toBeNull()
})
