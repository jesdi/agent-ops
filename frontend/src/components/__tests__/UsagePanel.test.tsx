import { render, screen } from '@testing-library/react'
import { UsagePanel } from '../UsagePanel'
import { usage, usageCcusage, usageUnavailable } from '../../test/fixtures'

it('renders one bullet per window with used, allowance and remaining in the accessible text', () => {
  render(<UsagePanel providers={usage} />)
  const bars = screen.getAllByRole('progressbar')
  expect(bars).toHaveLength(3)
  expect(bars[2]).toHaveAccessibleName('anthropic Week · Fable used')
  expect(bars[2]).toHaveAttribute('aria-valuenow', '23')
  expect(bars[2]).toHaveAttribute('aria-valuetext', '23% used of 29% allowed now, 77% remaining, close to the limit')
  expect(screen.getByText('77% left')).toBeInTheDocument()
  expect(screen.getByText('headroom 5.9 pts')).toBeInTheDocument()
  expect(screen.getByText('cap 80%')).toBeInTheDocument()
})

it('the provider heading carries the spawn verdict', () => {
  render(<UsagePanel providers={usage} />)
  expect(screen.getByText('anthropic')).toBeInTheDocument()
  expect(screen.getByText('will spawn')).toBeInTheDocument()
})

it('unavailable provider states the consequence, not an empty gauge', () => {
  render(<UsagePanel providers={usageUnavailable} />)
  expect(screen.getByText('usage unknown — dispatcher will not spawn on anthropic')).toBeInTheDocument()
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})

it('a ccusage reading says the weekly windows are unknown', () => {
  render(<UsagePanel providers={usageCcusage} />)
  expect(screen.getAllByRole('progressbar')).toHaveLength(1)
  expect(screen.getByText('via ccusage · weekly unknown')).toBeInTheDocument()
})

it('a blocked window names itself', () => {
  const blocked = [{ ...usage[0]!, would_spawn: false,
    windows: [{ ...usage[0]!.windows[1]!, used: 0.4, headroom: -0.111, severity: 'blocked' }] }]
  render(<UsagePanel providers={blocked} />)
  expect(screen.getByText('will not spawn')).toBeInTheDocument()
  expect(screen.getByRole('progressbar')).toHaveAttribute(
    'aria-valuetext', '40% used of 29% allowed now, 60% remaining, over the limit')
  expect(screen.getByText('headroom -11.1 pts')).toBeInTheDocument()
})
