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
    expect(stageLabel('spec-review')).toBe('Spec review')
    expect(stageLabel('some-new-stage')).toBe('some-new-stage')
  })
})
