import { Link, useNavigate, useParams } from 'react-router'
import { OnboardingForm, panel, primary } from '../components/onboarding/OnboardingForm'

import { useOnboarding, useOnboardings } from '../hooks/useOnboarding'
import { banner, chip } from '../lib/tone'
import { OnboardingDetail, PendingOnboarding } from '../components/onboarding/OnboardingDetail'
import type { OnboardingView } from '../lib/api'

export function NewOnboardingPage() {
  const navigate = useNavigate()
  return <main className="mx-auto w-full max-w-6xl px-4 py-7 md:px-7">
    <Link to="/onboarding" className="text-sm text-ink-muted underline">Onboarding</Link>
    <h1 className="mt-4 text-3xl font-semibold tracking-tight">Add project</h1>
    <p className="mb-7 mt-2 text-ink-muted">Connect a board and its repositories to the box.</p>
    <OnboardingForm onSaved={project => navigate(`/onboarding/projects/${encodeURIComponent(project)}`)} />
  </main>
}

export function OnboardingsPage() {
  const query = useOnboardings()
  return <main className="mx-auto w-full max-w-6xl px-4 py-7 md:px-7">
    <div className="mb-7 flex flex-wrap items-center justify-between gap-4"><div><h1 className="text-3xl font-semibold tracking-tight">Onboarding</h1>
      <p className="mt-2 text-ink-muted">Projects started here or from the terminal.</p></div><Link to="/onboarding/new" className={primary}>Add project</Link></div>
    {query.isPending && <p role="status">Loading onboardings…</p>}
    {query.error && <p role="alert" className={banner.failed}>{query.error.message}</p>}
    {query.data && <OnboardingList onboardings={query.data.onboardings} />}
  </main>
}

function OnboardingList({ onboardings }: { onboardings: OnboardingView[] }) {
  if (!onboardings.length) return <div className={panel}><h2 className="text-xl font-semibold">No onboardings yet</h2><p className="mt-2 text-ink-muted">Add a project to connect its board and repositories.</p></div>
  return <ul className={`${panel} divide-y divide-border`}>
    {onboardings.map(item => <li key={item.project} className="py-4 first:pt-0 last:pb-0"><Link to={`/onboarding/projects/${encodeURIComponent(item.project)}`} className="flex items-center justify-between gap-4">
      <div><span className="font-semibold underline">{item.project}</span><p className="mt-1 text-sm text-ink-muted">{item.request.board.owner}/{item.request.board.number} · {item.request.repos.length} repositories</p></div>
      <span className={chip.neutral}>{item.status}</span>
    </Link></li>)}
  </ul>
}

export function OnboardingPage() {
  const { project = '' } = useParams()
  const query = useOnboarding(project)
  return <main className="mx-auto w-full max-w-6xl px-4 py-7 md:px-7">
    <Link to="/onboarding" className="text-sm text-ink-muted underline">All onboardings</Link>
    <h1 className="mb-7 mt-4 text-3xl font-semibold tracking-tight">{project}</h1>
    {query.isPending && <p role="status">Loading onboarding…</p>}
    {query.error && <p role="alert" className={banner.failed}>{query.error.message}</p>}
    {query.data && (query.data.status === 'queued' ? <PendingOnboarding view={query.data} /> : <OnboardingDetail key={project} view={query.data} updatedAt={query.dataUpdatedAt} />)}
  </main>
}
