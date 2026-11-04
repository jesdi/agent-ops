import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Link } from 'react-router'
import { api, type OnboardingView } from '../../lib/api'
import { useOnboardingReceipt } from '../../hooks/useOnboarding'
import { banner, chip, type Tone } from '../../lib/tone'
import { RepositoryFacts, Source } from './InspectionFields'
import { OnboardingEdit } from './OnboardingEdit'
import { boardLabel, button, panel, primary, PausedNote } from './OnboardingForm'

const stateTone: Record<string, Tone> = { verified: 'running', waiting: 'neutral', blocked: 'waiting', failed: 'failed', pending: 'neutral' }
const stepNames: Record<string, string> = { resolve: 'Resolve repositories', clone: 'Clone repositories', inspect: 'Inspect repositories', confirm: 'Confirm proposal', board: 'Prepare board', 'config-pr': 'Create infrastructure PR', 'config-merge': 'Merge infrastructure PR', deploy: 'Deploy configuration', visible: 'Visible in console' }

function Steps({ steps }: { steps: OnboardingView['steps'] }) {
  return <section className={panel} aria-label="Onboarding progress"><h2 className="text-xl font-semibold">Onboarding progress</h2>
    <ol className="mt-5 divide-y divide-border">{steps.map(step => <li key={step.name} className="py-4 first:pt-0 last:pb-0">
      <div className="flex flex-wrap justify-between gap-2"><h3 className="font-medium">{stepNames[step.name] ?? step.name}</h3><span className={chip[stateTone[step.state] ?? 'neutral']}>{step.state}</span></div>
      {step.detail && <p className="mt-2 whitespace-pre-wrap break-words text-sm text-ink-muted">{step.detail}</p>}
      {step.link && <a className="mt-2 inline-block break-all text-sm underline" href={step.link}>Open {stepNames[step.name] ?? step.name} link</a>}
    </li>)}</ol>
  </section>
}

function ProposalRepositories({ snapshot }: { snapshot: OnboardingView }) {
  return <div className="mt-5 divide-y divide-border">{snapshot.proposal!.repositories.map(repo => {
    const inspection = snapshot.inspection?.repositories?.find(item => item.repo === repo.repo)
    return <div key={repo.github_id} className="space-y-4 py-5 first:pt-0">
      <h3 className="break-words font-semibold">{repo.repo}</h3>
      <dl className="space-y-3 text-sm">{([['Setup', 'setup_cmd'], ['Gate', 'gate_cmd'], ['Verification', 'verify_cmd']] as const).map(([name, key]) => <div key={key}>
        <dt className="text-ink-muted">{name}</dt><dd className="mt-1 whitespace-pre-wrap break-words font-mono">{repo[key] || 'Not set'}</dd>
        <Source source={inspection?.fields[key]?.source} inspected={inspection?.state === 'complete'} skipped={inspection?.state === 'skipped'} />
      </div>)}</dl>
      {inspection && <RepositoryFacts inspection={inspection} selected={repo.services ?? []} />}
    </div>
  })}</div>
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
    <div><p className="font-semibold">{proposal.name}</p><p className="text-sm text-ink-muted">GitHub board</p><p>{boardLabel(proposal.board, proposal.name) || 'Not set'}</p>{!(proposal.board && 'new' in proposal.board) && <Source source={snapshot.inspection?.repositories?.find(item => item.board.source)?.board.source} inspected={Boolean(snapshot.inspection?.repositories?.length)} />}</div>
    {snapshot.board_review && snapshot.board_review.length > 0 && <div><h3 className="text-sm text-ink-muted" id="board-changes">Board changes</h3>
      <ul aria-labelledby="board-changes" className="mt-1 list-disc space-y-1 pl-5 text-sm">{snapshot.board_review.map(line => <li key={line}>{line}</li>)}</ul></div>}
    <ProposalRepositories snapshot={snapshot} />
    <details className="text-sm"><summary className="cursor-pointer text-ink-muted">Configuration details</summary>
      <p className="mt-3 break-all text-xs text-ink-muted">Revision: {snapshot.revision}</p>
      <pre className="mt-2 overflow-x-auto rounded border border-border bg-surface p-3 text-xs">{JSON.stringify(proposal, null, 2)}</pre>
    </details><PausedNote />
    {changed && <p role="alert" className={banner.waiting}>The proposal changed while you were reviewing it. Review the latest proposal before confirming.</p>}
    {!current.can_confirm && !noLongerNeeded && <p role="status" className={banner.waiting}>Choose a board and fill in a gate command for every repository before confirming.</p>}
    <div className="flex flex-wrap justify-end gap-3"><button type="button" className={button} onClick={close}>Close review</button>
      {noLongerNeeded ? <p role="status" className="text-sm text-ink-muted">This proposal no longer needs confirmation.</p> : changed ? <button type="button" className={primary} onClick={refresh} disabled={busy}>Review latest proposal</button>
        : <button type="button" className={primary} onClick={confirm} disabled={busy || !current.can_confirm}>Confirm proposal</button>}
    </div>
  </section>
}

export function PendingOnboarding({ view }: { view: OnboardingView }) {
  return <section className={`${panel} space-y-3`}><h2 className="text-xl font-semibold">Waiting for the box</h2>
    <p className="text-sm text-ink-muted">Your request is saved. This page updates as the box works through onboarding.</p>
    <ul className="space-y-2 text-sm">{view.pending.map(item => <li key={item.id}>Requested by {item.actor}: {item.action}</li>)}</ul>
  </section>
}

function ActionButtons({ view, edit, busy, review, resume, skip }: {
  view: OnboardingView; edit: () => void; busy: boolean; review: () => void; resume: () => void; skip: () => void
}) {
  return (
    <div className="flex flex-wrap gap-3">
      {view.edit_mode !== 'none' && <button type="button" className={button} onClick={edit} disabled={busy}>Edit details</button>}
      {view.steps.some(step => step.name === 'inspect' && step.state === 'waiting') && <button type="button" className={button} onClick={skip} disabled={busy}>Fill in by hand</button>}
      {view.status === 'open' && <button type="button" className={button} onClick={resume} disabled={busy}>Resume onboarding</button>}
      {view.awaiting === 'confirm' && view.proposal && <button type="button" className={primary} onClick={review} disabled={busy}>Review proposal</button>}
    </div>
  )
}

function OnboardingActions({ view, updatedAt, edit }: { view: OnboardingView; updatedAt: number; edit: () => void }) {
  const receipt = useOnboardingReceipt(view, updatedAt)
  const [review, setReview] = useState<OnboardingView | null>(null)
  const submitAction = (action: { kind: 'confirm'; revision: string } | { kind: 'resume' } | { kind: 'skip'; revision: string }) => {
    if (action.kind === 'confirm') return api.confirmOnboarding(view.project, action.revision)
    if (action.kind === 'skip') return api.skipOnboardingInspection(view.project, action.revision)
    return api.resumeOnboarding(view.project)
  }
  const mutation = useMutation({ mutationFn: submitAction,
    onSuccess: accepted => { setReview(null); return receipt.remember(accepted, view.project) } })
  const busy = mutation.isPending || receipt.pending || view.pending.length > 0
  return <>
    {receipt.pending && view.pending.length === 0 && <PendingOnboarding view={view} />}
    {mutation.error && <p role="alert" className={banner.failed}>{mutation.error.message}</p>}
    <ActionButtons view={view} edit={edit} busy={busy} review={() => setReview(view)} resume={() => mutation.mutate({ kind: 'resume' })} skip={() => mutation.mutate({ kind: 'skip', revision: view.revision })} />
    {review && <ProposalReview snapshot={review} current={view} busy={busy} close={() => setReview(null)} refresh={() => setReview(view)} confirm={() => mutation.mutate({ kind: 'confirm', revision: review.revision })} />}
  </>
}

export function OnboardingDetail({ view, updatedAt }: { view: OnboardingView; updatedAt: number }) {
  const [editing, setEditing] = useState(false)
  if (editing) return <OnboardingEdit view={view} updatedAt={updatedAt} close={() => setEditing(false)} />
  return <div className="space-y-5">
    {view.pending.length > 0 && <PendingOnboarding view={view} />}
    {view.notice && <p role="status" className={banner.waiting}>{view.notice}</p>}
    <Steps steps={view.steps} />
    {view.pr_url && <a href={view.pr_url} className="inline-block text-sm underline">Open infrastructure PR</a>}
    <OnboardingActions view={view} updatedAt={updatedAt} edit={() => setEditing(true)} />
    {view.status === 'complete' && <p className={banner.running}>Onboarding complete. The project starts paused. <Link className="underline" to={`/?project=${encodeURIComponent(view.project)}`}>View project</Link></p>}
  </div>
}
