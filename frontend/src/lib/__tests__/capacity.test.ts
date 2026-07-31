import { capacityAccent } from '../capacity'

it('is blue while there is headroom', () => {
  expect(capacityAccent({ active: 2, capacity: 3 })).toBe('blue')
})

it('is amber exactly at full — the state that explains a stalled dispatcher', () => {
  expect(capacityAccent({ active: 3, capacity: 3 })).toBe('amber')
})

it('is amber when active overshoots a lowered capacity', () => {
  expect(capacityAccent({ active: 3, capacity: 2 })).toBe('amber')
})

it('is blue when nothing is running', () => {
  expect(capacityAccent({ active: 0, capacity: 3 })).toBe('blue')
})
