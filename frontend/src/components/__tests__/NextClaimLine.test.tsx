import { act, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import { NextClaimLine } from '../NextClaimLine'
import type { NextClaimView } from '../../lib/api'

const base: NextClaimView = {
  verdict: 'will-claim', next_pass_eta: '', next_issue: 73,
  next_target: 'alpha', minutes_to_reset: 0,
}

function withEta(secondsFromNow: number): NextClaimView {
  return { ...base, next_pass_eta: new Date(Date.now() + secondsFromNow * 1000).toISOString() }
}

test('will-claim renders countdown and the next issue', () => {
  render(<NextClaimLine nextClaim={withEta(360)} />)
  expect(screen.getByTestId('next-claim').textContent).toMatch(/next pass in 6m/i)
  expect(screen.getByTestId('next-claim').textContent).toMatch(/will claim #73/)
})

test('countdown ticks down and clamps to due now', () => {
  vi.useFakeTimers()
  render(<NextClaimLine nextClaim={withEta(2)} />)
  act(() => vi.advanceTimersByTime(5000))
  expect(screen.getByTestId('next-claim').textContent).toMatch(/due now/i)
  vi.useRealTimers()
})

test.each([
  [{ ...withEta(300), verdict: 'budget-blocked', minutes_to_reset: 130 }, /budget resets in 2h 10m/i],
  [{ ...withEta(300), verdict: 'capacity-full' }, /capacity full/i],
  [{ ...withEta(300), verdict: 'no-candidates' }, /queue empty/i],
  [{ ...base, verdict: 'unknown' }, /dispatcher not running\?/i],
  [{ ...withEta(300), verdict: 'claims-paused' }, /claiming paused — triage sweep pending/i],
])('verdict copy: %#', (nc, pattern) => {
  render(<NextClaimLine nextClaim={nc as NextClaimView} />)
  expect(screen.getByTestId('next-claim').textContent).toMatch(pattern)
})
