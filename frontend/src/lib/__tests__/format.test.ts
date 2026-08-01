import { formatUtilization, relativeTime, stageLabel } from '../format'

describe('relativeTime', () => {
  const now = new Date('2026-07-25T12:00:00Z')
  it('renders seconds, minutes, hours, days', () => {
    expect(relativeTime('2026-07-25T11:59:30Z', now)).toBe('30s ago')
    expect(relativeTime('2026-07-25T11:45:00Z', now)).toBe('15m ago')
    expect(relativeTime('2026-07-25T09:00:00Z', now)).toBe('3h ago')
    expect(relativeTime('2026-07-22T12:00:00Z', now)).toBe('3d ago')
  })
  it('clamps future timestamps to "just now"', () => {
    expect(relativeTime('2026-07-25T12:00:05Z', now)).toBe('just now')
  })
})

describe('formatUtilization', () => {
  it('renders a 0..1 fraction as a percent', () => {
    expect(formatUtilization(0.625)).toBe('63%')
    expect(formatUtilization(0)).toBe('0%')
    expect(formatUtilization(1)).toBe('100%')
  })
})

describe('stageLabel', () => {
  it('maps known stages and passes unknown through', () => {
    expect(stageLabel('implement')).toBe('Implementing')
    expect(stageLabel('some-new-stage')).toBe('some-new-stage')
  })

  // Keys must be real dispatcher Stage values, not lookalikes: 'pr' and
  // 'spec-review' matched no stage, so those cards rendered the raw slug.
  it('labels the real Stage values the console renders', () => {
    expect(stageLabel('awaiting-spec-review')).toBe('Spec review')
    expect(stageLabel('pr-open')).toBe('PR open')
    expect(stageLabel('address-review')).toBe('Addressing review')
    expect(stageLabel('spec')).toBe('Writing spec')
    expect(stageLabel('done')).toBe('Done')
  })
})

import { formatDuration } from '../format'

test('formatDuration picks the two most significant units', () => {
  expect(formatDuration(45)).toBe('45s')
  expect(formatDuration(720)).toBe('12m')
  expect(formatDuration(8100)).toBe('2h 15m')
  expect(formatDuration(273600)).toBe('3d 4h')
  expect(formatDuration(0)).toBe('0s')
})
