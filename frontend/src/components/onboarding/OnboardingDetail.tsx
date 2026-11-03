import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api, type OnboardingView, type OnboardingProposal } from '../../lib/api'
import { onboardingKeys } from '../../hooks/useOnboarding'
import { banner, chip, type Tone } from '../../lib/tone'
import { button, panel, primary, PausedNote, OnboardingForm } from './OnboardingForm'

const stateTone: Record<string, Tone> = { verified: 'running', waiting: 'neutral', blocked: 'waiting', failed: 'failed', pending: 'neutral' }
const stepNames: Record<string, string> = { resolve: 'Resolve repositories', clone: 'Clone repositories', confirm: 'Confirm proposal', 'config-pr': 'Create infrastructure PR', 'config-merge': 'Merge infrastructure PR', deploy: 'Deploy configuration', visible: 'Visible in console' }

function Steps({ steps }: { steps: OnboardingView['steps'] }) {
  return <section className={panel} aria-label="Onboarding progress"><h2 className="text-xl font-semibold">Onboarding progress</h2>
    <ol className="mt-5 divide-y divide-border">{steps.map(step => <li key={step.name} className="py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap justify-between gap-2"><h3 className="font-medium">{stepNames[step.name] ?? step.name}</h3><span className={chip[stateTone[step.state] ?? 'neutral']}>{step.state}</span></div>
      {step.detail && <p className="mt-2 whitespace-pre-wrap break-words text-sm text-ink-muted">{step.detail}</p>}
      {step.link && <a className="mt-2 inline-block break-all text-sm underline" href={step.link}>Open {stepNames[step.name] ?? step.name} link</a>}
    </li>)}</ol>
  </section>
}

function ProposalRepositories({ proposal }: { proposal: OnboardingProposal }) {
  return <div className="mt-5 divide-y divide-border">{proposal.repositories.map(repo => <div key={repo.github_id} className="py-5 first:pt-0">
    <h3 className="break-words font-semibold">{repo.repo}</h3>
    <dl className="mt-3 space-y-3 text-sm">{([['Setup', repo.setup_cmd], ['Gate', repo.gate_cmd], ['Verification', repo.verify_cmd]] as const).map(([name, command]) => <div key={name}>
      <dt className="text-ink-muted">{name}</dt><dd className="mt-1 whitespace-pre-wrap break-words font-mono">{command || 'Skipped'}</dd>
    </div>)}</dl>
  </div>)}</div>
}

function ProposalReview({ snapshot, current, busy, confirm, refresh, close }: {
  snapshot: OnboardingView; current: OnboardingView; busy: boolean
  confirm: () => void; refresh: () => void; close: () => void
}) {
  const proposal = snapshot.proposal!
  const changed = snapshot.revision !== current.revision
  const noLongerNeeded = current.awaiting !== 'confirm' || current.status !== 'open'
  return <section className={`${panel} space-y-4`} aria-label="Review proposal">
    <h2 className="text-xl font-semibold">Review proposal</h2>
    <div><p className="font-semibold">{proposal.name}</p><p className="text-sm text-ink-muted">GitHub board</p><p>{proposal.board.owner}/{proposal.board.number}</p></div>
    <ProposalRepositories proposal={proposal} />
    <details className="text-sm"><summary className="cursor-pointer text-ink-muted">Configuration details</summary>
      <p className="mt-3 break-all text-xs text-ink-muted">Revision: {snapshot.revision}</p>
      <pre className="mt-2 overflow-x-auto rounded border border-border bg-surface p-3 text-xs">{JSON.stringify(proposal, null, 2)}</pre>
    </details><PausedNote />
    {changed && <p role="alert" className={banner.waiting}>The proposal changed while you were reviewing it. Review the latest proposal before confirming.</p>}
    <div className="flex flex-wrap justify-end gap-3"><button type="button" className={button} onClick={close}>Close review</button>
      {noLongerNeeded ? <p role="status" className="text-sm text-ink-muted">This proposal no longer needs confirmation.</p> : changed ? <button type="button" className={primary} onClick={refresh} disabled={busy}>Review latest proposal</button>
        : <button type="button" className={primary} onClick={confirm} disabled={busy}>Confirm proposal</button>}
    </div>
  </section>
}

export function PendingOnboarding({ view }: { view: OnboardingView }) {
  return <section className={`${panel} space-y-3`}><h2 className="text-xl font-semibold">Waiting for the box</h2>
    <p className="text-sm text-ink-muted">Your request is saved. This page updates as the box works through onboarding.</p>
    <ul className="space-y-2 text-sm">{view.pending.map(item => <li key={item.id}>Requested by {item.actor}: {item.action}</li>)}</ul>
  </section>
}

function editable(view: OnboardingView): boolean {
  return view.status === 'open' && !view.steps.some(step => step.name === 'config-merge' && step.state === 'verified')
}

function OnboardingActions({ view, edit }: { view: OnboardingView; edit: () => void }) {
  const client = useQueryClient()
  const [review, setReview] = useState<OnboardingView | null>(null)
  const submitAction = (action: { kind: 'confirm'; revision: string } | { kind: 'resume' }) =>
    action.kind === 'confirm' ? api.confirmOnboarding(view.project, action.revision) : api.resumeOnboarding(view.project)
  const mutation = useMutation({ mutationFn: submitAction,
    onSuccess: async () => { setReview(null); await client.invalidateQueries({ queryKey: onboardingKeys.all }) } })
  const busy = mutation.isPending || view.pending.length > 0
  return <>
    {mutation.error && <p role="alert" className={banner.failed}>{mutation.error.message}</p>}
    <div className="flex flex-wrap gap-3">
      {editable(view) && <button type="button" className={button} onClick={edit} disabled={busy}>Edit details</button>}
      {view.status === 'open' && <button type="button" className={button} onClick={() => mutation.mutate({ kind: 'resume' })} disabled={busy}>Resume onboarding</button>}
      {view.awaiting === 'confirm' && view.proposal && <button type="button" className={primary} onClick={() => setReview(view)} disabled={busy}>Review proposal</button>}
    </div>
    {review && <ProposalReview snapshot={review} current={view} busy={busy} close={() => setReview(null)} refresh={() => setReview(view)} confirm={() => mutation.mutate({ kind: 'confirm', revision: review.revision })} />}
  </>
}

export function OnboardingDetail({ view }: { view: OnboardingView }) {
  const client = useQueryClient()
  const [editing, setEditing] = useState<OnboardingView | null>(null)
  if (editing && editable(view)) return <OnboardingForm project={view.project} request={editing.request} repositoriesLocked={Boolean(view.confirmed_revision)}
    cancel={<button type="button" className={button} onClick={() => setEditing(null)}>Cancel editing</button>}
    onSaved={async () => { await client.invalidateQueries({ queryKey: onboardingKeys.all }); setEditing(null) }} />
  return <div className="space-y-5">
    {view.pending.length > 0 && <PendingOnboarding view={view} />}
    {view.notice && <p role="status" className={banner.waiting}>{view.notice}</p>}
    <Steps steps={view.steps} />
    {view.pr_url && <a href={view.pr_url} className="inline-block text-sm underline">Open infrastructure PR</a>}
    <OnboardingActions view={view} edit={() => setEditing(view)} />
    {view.status === 'complete' && <p className={banner.running}>Onboarding complete. The project starts paused. <Link className="underline" to={`/?project=${encodeURIComponent(view.project)}`}>View project</Link></p>}
  </div>
}
