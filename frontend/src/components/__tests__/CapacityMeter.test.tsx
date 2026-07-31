import { render, screen } from '@testing-library/react'
import { CapacityMeter } from '../CapacityMeter'

function pips() {
  return screen.getAllByTestId(/^cap-pip-/)
}

it('renders one pip per capacity unit, filled up to active', () => {
  render(<CapacityMeter capacity={{ active: 1, capacity: 3, slots_used: 1, max_slots: 3 }} />)
  expect(pips()).toHaveLength(3)
  expect(screen.getAllByTestId('cap-pip-filled')).toHaveLength(1)
  expect(screen.getAllByTestId('cap-pip-empty')).toHaveLength(2)
})

it('fills every pip at full capacity', () => {
  render(<CapacityMeter capacity={{ active: 3, capacity: 3, slots_used: 3, max_slots: 3 }} />)
  expect(screen.getAllByTestId('cap-pip-filled')).toHaveLength(3)
  expect(screen.queryAllByTestId('cap-pip-empty')).toHaveLength(0)
})

it('keeps the text truthful when active overshoots a lowered capacity', () => {
  render(<CapacityMeter capacity={{ active: 3, capacity: 2, slots_used: 2, max_slots: 3 }} />)
  expect(pips()).toHaveLength(2)
  expect(screen.getAllByTestId('cap-pip-filled')).toHaveLength(2)
  expect(screen.getByText(/3\/2 active/)).toBeInTheDocument()
})

it('AWKWARD: falls back to text only when capacity is misconfigured to zero', () => {
  render(<CapacityMeter capacity={{ active: 0, capacity: 0, slots_used: 0, max_slots: 3 }} />)
  expect(screen.queryAllByTestId(/^cap-pip-/)).toHaveLength(0)
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  expect(screen.getByText(/0\/0 active/)).toBeInTheDocument()
})

it('still reports slots as plain text', () => {
  render(<CapacityMeter capacity={{ active: 2, capacity: 3, slots_used: 2, max_slots: 3 }} />)
  expect(screen.getByText(/slots 2\/3/)).toBeInTheDocument()
})

it('exposes the occupancy to assistive tech', () => {
  render(<CapacityMeter capacity={{ active: 2, capacity: 3, slots_used: 2, max_slots: 3 }} />)
  const bar = screen.getByRole('progressbar', { name: 'capacity units in use' })
  expect(bar).toHaveAttribute('aria-valuenow', '2')
  expect(bar).toHaveAttribute('aria-valuemax', '3')
  expect(bar).toHaveAttribute('aria-valuetext', '2 of 3 capacity units in use')
})

it('clamps aria-valuenow in overflow case but keeps aria-valuetext truthful', () => {
  render(<CapacityMeter capacity={{ active: 3, capacity: 2, slots_used: 2, max_slots: 3 }} />)
  const bar = screen.getByRole('progressbar', { name: 'capacity units in use' })
  expect(bar).toHaveAttribute('aria-valuenow', '2')
  expect(bar).toHaveAttribute('aria-valuemax', '2')
  expect(bar).toHaveAttribute('aria-valuetext', '3 of 2 capacity units in use')
})
