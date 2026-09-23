import type { MessageView } from '../lib/api'
import { relativeTime } from '../lib/format'

// State is never colour-only: each chip carries its word. "sending" means the
// intent file exists but no dispatcher pass has drained it; "queued" means it
// is in the durable message file; "delivered" means a session actually got it.
const CHIP: Record<string, string> = {
  sending: 'bg-ink/10 text-ink-muted',
  queued: 'bg-ink/10 text-ink-muted',
  delivered: 'bg-ink/10 text-ink',
}

export function MessageThread({ messages }: { messages: MessageView[] }) {
  if (messages.length === 0) return null
  return (
    <div data-testid="message-thread" className="flex flex-col gap-2">
      {messages.map((m) => (
        <div
          key={m.id}
          data-testid={`message-${m.id}`}
          className="rounded border border-border bg-surface-raised px-3 py-2 text-sm"
        >
          <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
            <span>{m.actor || 'operator'}</span>
            {m.created_at !== '' && <span>{relativeTime(m.created_at)}</span>}
            <span
              data-testid="message-state"
              className={`rounded px-1.5 ${CHIP[m.state] ?? CHIP.queued}`}
            >
              {m.state}
              {m.state === 'delivered' && m.delivered_at !== ''
                ? ` ${relativeTime(m.delivered_at)}`
                : ''}
            </span>
          </div>
          <p className="mt-1 whitespace-pre-wrap">{m.text}</p>
        </div>
      ))}
    </div>
  )
}
