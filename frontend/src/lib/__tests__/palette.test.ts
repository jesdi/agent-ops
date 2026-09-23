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
