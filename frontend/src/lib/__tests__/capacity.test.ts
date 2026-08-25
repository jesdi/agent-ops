import { capacityAccent, SLOT_COLORS, slotBorder, slotChip, slotSegment } from '../capacity'

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

test('slot 0 gets the same colour everywhere', () => {
  expect(slotBorder(0)).toContain(SLOT_COLORS[0])
  expect(slotChip(0)).toContain(SLOT_COLORS[0])
  expect(slotSegment(0)).toContain(SLOT_COLORS[0])
})

test('a slot-less card gets no colour', () => {
  expect(slotBorder(-1)).toBe('')
  expect(slotChip(-1)).toBe('')
  expect(slotSegment(-1)).toBe('')
})

test('the palette cycles past its length instead of going blank', () => {
  expect(slotChip(SLOT_COLORS.length)).toBe(slotChip(0))
})

test('every class string is a full literal Tailwind can scan', () => {
  for (let i = 0; i < SLOT_COLORS.length; i++) {
    expect(slotBorder(i)).not.toContain('${')
    expect(slotChip(i)).toMatch(/^bg-\S+ text-\S+$/)
  }
})
