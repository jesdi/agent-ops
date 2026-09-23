import type { MessageView } from '../lib/api'
import { relativeTime } from '../lib/format'
import { chip, type Tone } from '../lib/tone'

const STATE_TONE: Record<string, Tone> = { queued: 'waiting', delivered: 'running' }

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
            {/* The chip carries the state's word. "sending" means the intent
                file exists but no dispatcher pass has drained it; "queued"
                means it is in the durable message file; "delivered" means a
                session actually got it. */}
            <span data-testid="message-state" className={chip[STATE_TONE[m.state] ?? 'neutral']}>
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
