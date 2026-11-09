import { defineConfig } from 'vitest/config'
import config from './vitest.config.ts'

// Vitest's positional file filters are substring matches. Exact include globs
// prevent selecting a.test.ts from also running a.test.tsx or a prefix sibling.
const files: string[] = JSON.parse(process.env.FAST_GATE_VITEST_FILES ?? '[]')
export default defineConfig({
  ...config,
  test: {
    ...config.test,
    include: files.map(file => file.replace(/[?*[\]{}()!+@]/g, '\\$&')),
  },
})
