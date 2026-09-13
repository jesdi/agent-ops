import { lazy } from 'react'

// Keep secondary pages and the Markdown renderer out of the board bundle.
export const FailuresPage = lazy(() => import('./FailuresPage').then((m) => ({ default: m.FailuresPage })))
export const HistoryPage = lazy(() => import('./HistoryPage').then((m) => ({ default: m.HistoryPage })))
export const TaskPage = lazy(() => import('./TaskPage').then((m) => ({ default: m.TaskPage })))
