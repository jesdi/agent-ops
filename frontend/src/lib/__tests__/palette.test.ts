// index.css deletes Tailwind's default palette (`--color-*: initial`), so a
// raw palette class would silently render with no colour. Tokens only.
const sources = import.meta.glob(['../../**/*.{ts,tsx}', '!../../**/__tests__/**'], {
  query: '?raw', import: 'default', eager: true,
}) as Record<string, string>

const HUES = 'slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose'
const RAW = new RegExp(`\\b[a-z-]+-(?:(?:${HUES})-\\d{2,3}|black|white)(?:/\\d+)?\\b`, 'g')

test('no source file uses a raw Tailwind palette class', () => {
  expect(Object.keys(sources).length).toBeGreaterThan(20)
  const hits = Object.entries(sources).flatMap(([file, text]) =>
    [...text.matchAll(RAW)].map((m) => `${file}: ${m[0]}`))
  expect(hits).toEqual([])
})

// Status tone lives in lib/tone.ts: a status fill or the neutral chip fill
// anywhere else is a hand-rolled chip or banner that will drift. Status text
// and dot colours (`text-failed-fg`, `bg-running-fg`) stay free.
const TONE_FILL = /\b(?:bg-(?:running|waiting|failed|parked)-bg|bg-ink\/10)\b/g
const TONE_FILL_ALLOWED: Record<string, string> = {
  '../../components/UsagePanel.tsx': 'bg-ink/10', // usage bar track, not a chip
  // ponytail: follow-up converts BoardPage's queue-stale/error chips to tone.
  '../../pages/BoardPage.tsx': 'bg-waiting-bg bg-failed-bg',
}

test('status chips and banners come only from lib/tone.ts', () => {
  const hits = Object.entries(sources)
    .filter(([file]) => file !== '../tone.ts')
    .flatMap(([file, text]) => [...text.matchAll(TONE_FILL)]
      .filter((m) => !(TONE_FILL_ALLOWED[file] ?? '').split(' ').includes(m[0]))
      .map((m) => `${file}: ${m[0]}`))
  expect(hits).toEqual([])
})
