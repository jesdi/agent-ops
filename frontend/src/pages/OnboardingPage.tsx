import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { boardLabel, OnboardingForm, panel, primary } from '../components/onboarding/OnboardingForm'

import { useOnboarding, useOnboardings, useOnboardingReceipt } from '../hooks/useOnboarding'
import { banner, chip } from '../lib/tone'
import { OnboardingDetail, PendingOnboarding } from '../components/onboarding/OnboardingDetail'
import type { OnboardingAccepted, OnboardingView } from '../lib/api'

export function NewOnboardingPage() {
  const navigate = useNavigate()
  const [project, setProject] = useState('')
  const query = useOnboarding(project)
  const receipt = useOnboardingReceipt(query.data, query.dataUpdatedAt)
  useEffect(() => {
    if (receipt.applied) void navigate(`/onboarding/projects/${encodeURIComponent(project)}`)
  }, [receipt.applied, navigate, project])
  const queued = async (name: string, accepted: OnboardingAccepted) => {
    setProject(name)
    await receipt.remember(accepted, name)
  }
  return <main className="mx-auto w-full max-w-6xl px-4 py-7 md:px-7">
    <Link to="/onboarding" className="text-sm text-ink-muted underline">Onboarding</Link>
    <h1 className="mt-4 text-3xl font-semibold tracking-tight">Add project</h1>
    <p className="mb-7 mt-2 text-ink-muted">Connect a board and its repositories to the box.</p>
    <OnboardingForm onSaved={queued} disabled={receipt.pending}
      feedback={<StartFeedback project={project} pending={receipt.pending} accepted={receipt.accepted !== null && !receipt.applied} error={query.error} />} />
  </main>
}

function StartFeedback({ project, pending, accepted, error }: {
  project: string; pending: boolean; accepted: boolean; error: Error | null
}) {
  if (!accepted) return null
  if (pending) return <div role="status" className={banner.waiting}><p className="font-semibold">Waiting for the box</p>
    <p>Your request is saved. Keep this page open while the box applies it.</p>
    {error && <p className="mt-2">Could not refresh onboarding: {error.message}. Checking again automatically.</p>}</div>
  return <div role="alert" className={banner.failed}>This project has different stored details. Your draft is kept. Choose another project name or <Link className="underline" to={`/onboarding/projects/${encodeURIComponent(project)}`}>Open existing onboarding</Link>.</div>
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
      <div><span className="font-semibold underline">{item.project}</span><p className="mt-1 text-sm text-ink-muted">{boardLabel(item.request.board, item.project) || 'Board not set'} · {item.request.repos.length} repositories</p></div>
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
