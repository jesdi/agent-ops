import { Link } from 'react-router'
import { useExamDebts } from '../hooks/useResources'

export function ExamDebtPage() {
  const { data, isPending, isError, error } = useExamDebts()
  if (isPending) return <p className="p-4 text-ink-muted">loading exam debt…</p>
  if (isError) return <p className="p-4 text-failed-fg">exam debt unavailable: {error.message}</p>
  if (data.length === 0) return <p className="p-4 text-ink-muted">No exam debt.</p>
  return (
    <ul className="space-y-2 p-4">
      {data.map((e) => (
        <li key={`${e.repo}#${e.pr}`} className="flex flex-wrap gap-x-3">
          <Link to={`/exam/${e.repo}/${e.pr}`} className="font-medium underline">{e.title ?? `PR #${e.pr}`}</Link>
          <span className="text-ink-muted">{e.repo} #{e.pr}</span>
          <span className="text-ink-muted">{e.state}</span>
        </li>
      ))}
    </ul>
  )
}
