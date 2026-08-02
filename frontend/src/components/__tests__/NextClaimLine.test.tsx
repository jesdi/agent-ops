import { act, render, screen } from '@testing-library/react'
import { NextClaimLine } from '../NextClaimLine'
import type { NextClaimView } from '../../lib/api'

// vi, afterEach, test, expect are vitest globals (globals: true in vitest.config.ts)

const base: NextClaimView = {
  verdict: 'will-claim', next_pass_eta: '', next_issue: 73,
  next_target: 'alpha', minutes_to_reset: 0,
}

function withEta(secondsFromNow: number): NextClaimView {
  return { ...base, next_pass_eta: new Date(Date.now() + secondsFromNow * 1000).toISOString() }
}

// Ensure fake timers never leak into subsequent tests regardless of assertion failures.
afterEach(() => {
  vi.useRealTimers()
})

test('will-claim renders countdown and the next issue', () => {
  // Pin the clock so elapsed time between withEta() and render is deterministically zero.
  vi.useFakeTimers()
  render(<NextClaimLine nextClaim={withEta(360)} />)
  expect(screen.getByTestId('next-claim').textContent).toMatch(/next pass in 6m/i)
  expect(screen.getByTestId('next-claim').textContent).toMatch(/will claim #73/)
})

test('countdown ticks down and clamps to due now', () => {
  vi.useFakeTimers()
  render(<NextClaimLine nextClaim={withEta(2)} />)
  act(() => vi.advanceTimersByTime(5000))
  expect(screen.getByTestId('next-claim').textContent).toMatch(/due now/i)
})

test('unparseable ETA renders honest copy, not a confident lie', () => {
  // next_pass_eta: '' produces NaN from getTime() — must not render "due now".
  render(<NextClaimLine nextClaim={{ ...base, verdict: 'capacity-full', next_pass_eta: '' }} />)
  expect(screen.getByTestId('next-claim').textContent).toMatch(/next pass time unknown/i)
  expect(screen.getByTestId('next-claim').textContent).not.toMatch(/due now/i)
})

test('unmounting clears the interval — clearInterval is called on unmount', () => {
  vi.useFakeTimers()
  const clearSpy = vi.spyOn(window, 'clearInterval')
  const { unmount } = render(<NextClaimLine nextClaim={withEta(60)} />)
  unmount()
  expect(clearSpy).toHaveBeenCalled()
  clearSpy.mockRestore()
})

test('unhandled verdict renders honest fallback instead of wrong copy', () => {
  // Cast through unknown to simulate a future seventh verdict the map doesn't contain.
  // `as unknown as NextClaimView` is the standard escape hatch for probing runtime
  // branches that TypeScript's type narrowing would otherwise block at the call site.
  const nc = { ...withEta(300), verdict: 'future-verdict-x' } as unknown as NextClaimView
  render(<NextClaimLine nextClaim={nc} />)
  expect(screen.getByTestId('next-claim').textContent).toMatch(/unknown verdict: future-verdict-x/)
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
