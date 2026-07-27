import { useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { queryKeys } from './queryKeys'

export const LiveUpdatesContext = createContext(false)

/** True while the single SSE connection is open; false = interval fallback. */
export function useLiveConnected(): boolean {
  return useContext(LiveUpdatesContext)
}

const CHANGED_TO_KEYS: Record<string, readonly (readonly string[])[]> = {
  board: [queryKeys.board, queryKeys.allTasks, queryKeys.pendingIntents],
  queue: [queryKeys.queue],
  budget: [queryKeys.budget],
  failures: [queryKeys.failures],
  history: [queryKeys.history],
}

export function LiveUpdatesProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [connected, setConnected] = useState(false)

  useEffect(() => {
    let source: EventSource | null = null
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let retryMs = 1000
    let disposed = false

    const connect = () => {
      source = new EventSource('/api/events')
      source.onopen = () => {
        retryMs = 1000
        setConnected(true)
      }
      source.onmessage = (e) => {
        // A malformed frame must not throw out of the handler — that would
        // drop this message's invalidations and leave the console stale.
        let changed: unknown
        try {
          changed = (JSON.parse(e.data as string) as { changed?: unknown }).changed
        } catch {
          return
        }
        if (!Array.isArray(changed)) return
        for (const resource of changed as string[]) {
          for (const key of CHANGED_TO_KEYS[resource] ?? []) {
            void queryClient.invalidateQueries({ queryKey: key })
          }
        }
      }
      source.onerror = () => {
        source?.close()
        setConnected(false)
        if (!disposed) {
          retryTimer = setTimeout(connect, retryMs)
          retryMs = Math.min(retryMs * 2, 30_000)
        }
      }
    }

    connect()
    return () => {
      disposed = true
      source?.close()
      clearTimeout(retryTimer)
    }
  }, [queryClient])

  return (
    <LiveUpdatesContext.Provider value={connected}>
      {children}
    </LiveUpdatesContext.Provider>
  )
}
