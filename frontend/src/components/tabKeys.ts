/** A tab row's keyboard: arrows wrap around, Home and End jump to the ends. */
export const TAB_STEP: Partial<Record<string, (index: number, last: number) => number>> = {
  ArrowRight: (i, last) => (i === last ? 0 : i + 1),
  ArrowLeft: (i, last) => (i === 0 ? last : i - 1),
  Home: () => 0,
  End: (_, last) => last,
}
