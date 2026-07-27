import { FitAddon } from '@xterm/addon-fit'
import { Terminal as XTerm } from '@xterm/xterm'
import '@xterm/xterm/css/xterm.css'
import { useEffect, useRef, useState } from 'react'

export function Terminal({ issue }: { issue: number }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [dead, setDead] = useState<{ tail: string } | null>(null)

  useEffect(() => {
    setDead(null)
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

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
    }
    ws.onmessage = (e) => {
      if (typeof e.data === 'string') {
        const msg = JSON.parse(e.data) as { type: string; tail?: string }
        if (msg.type === 'dead') setDead({ tail: msg.tail ?? '' })
        return
      }
      term.write(new Uint8Array(e.data as ArrayBuffer))
    }

    const dataSub = term.onData((d) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(new TextEncoder().encode(d))
      }
    })
    const resizeSub = term.onResize(({ cols, rows }) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'resize', cols, rows }))
      }
    })
    const observer = new ResizeObserver(() => fit.fit())
    observer.observe(el)

    return () => {
      observer.disconnect()
      dataSub.dispose()
      resizeSub.dispose()
      ws.close()
      term.dispose()
    }
  }, [issue])

  if (dead) {
    return (
      <div
        data-testid="terminal-dead"
        className="rounded border border-amber-300 bg-amber-50 p-3"
      >
        <p className="text-sm font-medium text-amber-800">
          session task-{issue} is not running
        </p>
        <pre className="mt-2 max-h-64 overflow-auto rounded bg-gray-900 p-2 font-mono text-xs text-gray-100">
          {dead.tail}
        </pre>
      </div>
    )
  }
  return (
    <div
      ref={containerRef}
      data-testid="terminal"
      className="h-96 w-full overflow-hidden rounded bg-black p-1"
    />
  )
}
