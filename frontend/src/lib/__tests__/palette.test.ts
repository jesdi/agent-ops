/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { __unstable__loadDesignSystem } from 'tailwindcss'

// index.css deletes Tailwind's default palette (`--color-*: initial`), so a
// raw palette class such as bg-red-500 compiles to nothing and renders
// uncoloured. Compile index.css and check every colour utility the sources
// use through Tailwind's own scanner and compiler produces a rule.
const require = createRequire(import.meta.url)
const { Scanner } = createRequire(require.resolve('@tailwindcss/vite'))('@tailwindcss/oxide')
const srcDir = resolve(__dirname, '../..')
const twDir = dirname(require.resolve('tailwindcss/package.json'))

const COLOUR_UTILITY =
  /^(?:[a-z-]+:)*-?(?:bg|text|border(?:-[xytrbles])?|ring|ring-offset|outline|fill|stroke|from|via|to|divide(?:-[xy])?|placeholder|accent|caret|decoration|shadow)-[a-z][a-z-]*(?:-\d+)?(?:-fg|-bg)?(?:\/\d+)?$/

test('every colour utility in the sources compiles against index.css', async () => {
  const design = await __unstable__loadDesignSystem(readFileSync(resolve(srcDir, 'index.css'), 'utf8'), {
    base: srcDir,
    loadStylesheet: async (id: string, base: string) => {
      const path = id === 'tailwindcss' ? resolve(twDir, 'index.css') : resolve(base, id)
      return { path, base: dirname(path), content: readFileSync(path, 'utf8') }
    },
  })
  const candidates = new Scanner({ sources: [
    { base: srcDir, pattern: '**/*.{ts,tsx}', negated: false },
    { base: srcDir, pattern: '**/__tests__/**', negated: true },
  ] })
    .scan()
    .filter((c: string) => COLOUR_UTILITY.test(c))
  expect(candidates.length).toBeGreaterThan(20)
  const css = design.candidatesToCss(candidates)
  const dead = candidates.filter((_: string, i: number) => css[i] === null)
  expect(dead).toEqual([])
  expect(design.candidatesToCss(['bg-red-500'])).toEqual([null])
})
