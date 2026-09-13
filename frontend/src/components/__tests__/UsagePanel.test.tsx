import { render, screen, within } from '@testing-library/react'
import { UsagePanel } from '../UsagePanel'
import type { UsageView } from '../../lib/api'
import { usage, usageCcusage, usageUnavailable } from '../../test/fixtures'

it('renders one bullet per window with used, allowance and remaining in the accessible text', () => {
  render(<UsagePanel usage={usage} />)
  const bars = screen.getAllByRole('progressbar')
  expect(bars).toHaveLength(3)
  expect(bars[2]).toHaveAccessibleName('anthropic Week · Fable used')
  expect(bars[2]).toHaveAttribute('aria-valuenow', '23')
  expect(bars[2]).toHaveAttribute('aria-valuetext', '23% used of 29% allowed now, 77% remaining, close to the limit')
  expect(screen.getByText('77% left')).toBeInTheDocument()
  expect(screen.getByText('headroom 5.9 pts')).toBeInTheDocument()
  expect(screen.getByText('cap 80%')).toBeInTheDocument()
})

it('the provider heading carries the default model gate, with its note on hover', () => {
  render(<UsagePanel usage={usage} />)
  expect(screen.getByText('anthropic')).toBeInTheDocument()
  expect(screen.getByText('will spawn claude-opus-4-8')).toHaveAttribute('title', usage.gate.note)
})

it('unavailable provider states the consequence, not an empty gauge', () => {
  render(<UsagePanel usage={usageUnavailable} />)
  expect(screen.getByText('usage unknown — dispatcher will not spawn on anthropic')).toBeInTheDocument()
  expect(screen.getByText('will not spawn claude-opus-4-8')).toBeInTheDocument()
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})

it('a ccusage reading says the weekly windows are unknown', () => {
  render(<UsagePanel usage={usageCcusage} />)
  expect(screen.getAllByRole('progressbar')).toHaveLength(1)
  expect(screen.getByText('via ccusage · weekly unknown')).toBeInTheDocument()
})

it('a blocked window names itself', () => {
  const blocked: UsageView = {
    providers: [{ ...usage.providers[0]!,
      windows: [{ ...usage.providers[0]!.windows[1]!, used: 0.4, headroom: -0.111, severity: 'blocked' }] }],
    gate: { ...usage.gate, admitted: false },
  }
  render(<UsagePanel usage={blocked} />)
  expect(screen.getByText('will not spawn claude-opus-4-8')).toBeInTheDocument()
  expect(screen.getByRole('progressbar')).toHaveAttribute(
    'aria-valuetext', '40% used of 29% allowed now, 60% remaining, over the limit')
  expect(screen.getByText('headroom -11.1 pts')).toBeInTheDocument()
})

it('with two providers the chip shows once, on the gate provider only', () => {
  const twoProviders: UsageView = {
    providers: [
      ...usage.providers,
      { provider: 'nvidia', source: 'oauth', windows: [usage.providers[0]!.windows[0]!] },
    ],
    gate: { ...usage.gate, model: 'nvidia/claude-sonnet-4-6', provider: 'nvidia' },
  }
  render(<UsagePanel usage={twoProviders} />)
  expect(screen.getAllByText(/^will (not )?spawn/)).toHaveLength(1)
  const nvidia = screen.getByText('nvidia').closest('div')!
  expect(within(nvidia).getByText('will spawn claude-sonnet-4-6')).toBeInTheDocument()
})
