import { setupServer } from 'msw/node'

// Handlers are registered per-test (server.use(...)) or from the default
// sets each task adds next to its fixtures.
export const server = setupServer()
