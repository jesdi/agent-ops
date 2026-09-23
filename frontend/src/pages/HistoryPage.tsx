import { TimelineList } from '../components/TimelineList'
import { useTimeline } from '../hooks/useResources'

export function HistoryPage() {
  const historyQuery = useTimeline()
  if (historyQuery.isPending) return <p className="p-4 text-ink-muted">loading history…</p>
  if (historyQuery.isError) {
    return <p className="p-4 text-failed-fg">history unavailable: {historyQuery.error.message}</p>
  }
  return (
    <div className="p-4">
      <TimelineList events={historyQuery.data.events} />
    </div>
  )
}
