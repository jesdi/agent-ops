import { TimelineList } from '../components/TimelineList'
import { useTimeline } from '../hooks/useResources'

export function HistoryPage() {
  const historyQuery = useTimeline()
  if (historyQuery.isPending) return <p className="p-4 text-gray-500">loading history…</p>
  if (historyQuery.isError) {
    return <p className="p-4 text-red-600">history unavailable: {historyQuery.error.message}</p>
  }
  return (
    <div className="p-4">
      <TimelineList events={historyQuery.data.events} />
    </div>
  )
}
