import { useUsage } from '../hooks/useResources'
import { formatDuration } from '../lib/format'
import { banner } from '../lib/tone'

/** Alert banner above the Board header for unreadable or over-pace usage.
 *  Follows the shared usage query: no dismiss, timer or polling of its own. */
export function UsageBanner() {
  const { data } = useUsage()
  if (!data) return null
  if (data.auth_dark) {
    return (
      <div role="alert" data-testid="auth-dark-banner" className={banner.failed}>
        <p>Can't read usage: the provider login has probably expired</p>
        <p className="text-xs text-ink-muted">since {new Date(data.auth_dark.since).toLocaleString()}</p>
      </div>
    )
  }
  if (!data.gate.admitted && data.gate.binding) {
    return (
      <div role="alert" data-testid="usage-exhausted-banner" className={banner.waiting}>
        Usage over pace: claims paused. The window resets in {formatDuration(data.gate.minutes_to_reset * 60)}
      </div>
    )
  }
  return null
}
