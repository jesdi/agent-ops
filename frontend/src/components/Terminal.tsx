import { FitAddon } from '@xterm/addon-fit'
import { Terminal as XTerm } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { useEffect, useRef, useState } from 'react'
import { TerminalHistory } from './TerminalHistory'
import { usePersistedTerminalHeight } from '../hooks/usePersistedTerminalHeight'

export function Terminal({ issue }: { issue: number }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [dead, setDead] = useState<{ tail: string } | null>(null)
  const { ref: paneWrapRef, height: terminalHeight } = usePersistedTerminalHeight()
  // A dropped *connection* is not a dead *session*: the tmux session may well
  // still be running (roaming Tailscale, agent-ops-web.service restarting, a
  // 4401/4403 auth close). Tracked separately so the copy can say so.
  const [disconnected, setDisconnected] = useState(false)
  const [showHistory, setShowHistory] = useState(false)

  useEffect(() => {
    setDead(null)
    setDisconnected(false)
    setShowHistory(false)
    const el = containerRef.current
    if (!el) return

    const term = new XTerm({ fontSize: 13, scrollback: 5000 })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(el)
    fit.fit()

    const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const ws = new WebSocket(
      `${proto}://${window.location.host}/api/task/${issue}/terminal`,
    )
    ws.binaryType = 'arraybuffer'

    // `disposed` — this effect was torn down (unmount or issue change); its
    // own ws.close() must not paint a disconnect banner over the next issue.
    // `sessionDead` — the server told us the session is gone and we closed on
    // purpose; that is the dead state, not a dropped connection.
    // `closed` — the socket is no longer usable; stop forwarding keystrokes.
    let disposed = false
    let sessionDead = false
    let closed = false

    const handleClose = () => {
      closed = true
      if (!disposed && !sessionDead) setDisconnected(true)
    }
    ws.onclose = handleClose
    ws.onerror = handleClose

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {
        try {
          const msg = JSON.parse(e.data) as { type: string; tail?: string }
          if (msg.type === 'dead') {
            sessionDead = true
            setDead({ tail: msg.tail ?? '' })
            ws.close()
          }
        } catch {
          // ignore unparseable text frames
        }
        return
      }
      term.write(new Uint8Array(e.data as ArrayBuffer))
    }

    const dataSub = term.onData((d) => {
      if (!closed && ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(d))
      }
    })
    const resizeSub = term.onResize(({ cols, rows }) => {
      if (!closed && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    })
    const observer = new ResizeObserver(() => fit.fit())
    observer.observe(el)

    // Own the wheel: returning false makes xterm apply NEITHER default —
    // no SGR mouse reports (whose magnitude it discards) and no arrow keys
    // injected into the app. Scrolling up opens our own history view, which
    // scrolls natively (full trackpad precision, correct momentum).
    term.attachCustomWheelEventHandler((e) => {
      if (e.deltaY < 0) setShowHistory(true)
      return false
    })

    return () => {
      disposed = true
      observer.disconnect()
      dataSub.dispose()
      resizeSub.dispose()
      ws.close()
      term.dispose()
    }
  }, [issue])

  // The container div is always mounted so containerRef.current is never null
  // when the effect runs after an issue change. The dead fallback is rendered
  // as an absolute overlay on top of the (stale) terminal rather than replacing
  // the container — prevents a blank/unconnected terminal when navigating from
  // a dead session to a live one.
  return (
    <div
      ref={paneWrapRef}
      data-testid="terminal-pane-wrap"
      className="relative w-full resize-y overflow-auto rounded bg-black"
      style={{ height: terminalHeight }}
    >
      <div
        ref={containerRef}
        data-testid="terminal"
        className="h-full w-full overflow-hidden rounded bg-black p-1"
      />
      {dead && (
        <div
          data-testid="terminal-dead"
          className="absolute inset-0 rounded border border-amber-300 bg-amber-50 p-3"
        >
          <p className="text-sm font-medium text-amber-800">
            session task-{issue} is not running
          </p>
          <pre className="mt-2 max-h-64 overflow-auto rounded bg-gray-900 p-2 font-mono text-xs text-gray-100">
            {dead.tail}
          </pre>
        </div>
      )}
      {showHistory && (
        <div className="absolute inset-0">
          <TerminalHistory
            issue={issue}
            onClose={() => setShowHistory(false)}
          />
        </div>
      )}
      {!dead && disconnected && (
        <div
          data-testid="terminal-disconnected"
          className="absolute inset-0 rounded border border-red-300 bg-red-50 p-3"
        >
          <p className="text-sm font-medium text-red-800">
            terminal disconnected — reattach to continue
          </p>
          <p className="mt-2 text-xs text-red-700">
            the connection to task-{issue} dropped. The session may still be
            running; detach and attach again to reconnect.
          </p>
        </div>
      )}
    </div>
  )
}
