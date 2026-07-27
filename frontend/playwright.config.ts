import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  workers: 1, // the fake server holds mutable seed state
  use: { baseURL: 'http://127.0.0.1:8481' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'node e2e/fake-api.mjs',
    url: 'http://127.0.0.1:8481',
    // The fake server holds mutable seed state — reuse would leave a drained
    // board across runs. Always start fresh.
    reuseExistingServer: false,
  },
})
