import { defineConfig } from '@playwright/test'
import base from './playwright.config'

// Suite isolation check: the stateful flows run in a fixed order, forward or
// reverse (ORDER=reverse), against one live fake server for the whole run.
// Each file is its own project chained by `dependencies`, which fixes the
// order; both device projects run, one after the other.
const files = ['queue-flow', 'park-reply-flow', 'pinned-tracks', 'board']
if (process.env.ORDER === 'reverse') files.reverse()

let previous: string | undefined
const projects = base.projects!.flatMap((device) =>
  files.map((file) => {
    const name = `${device.name}:${file}`
    const project = {
      ...device,
      name,
      testMatch: `${file}.spec.ts`,
      dependencies: previous ? [previous] : [],
    }
    previous = name
    return project
  }),
)

export default defineConfig({ ...base, projects })
