import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { OnboardingAccepted, OnboardingView } from '../../lib/api'
import { onboardingKeys } from '../../hooks/useOnboarding'
import { banner } from '../../lib/tone'
import { button, OnboardingForm } from './OnboardingForm'

function EditFeedback({ receipt, pending, view }: {
  receipt: OnboardingAccepted | null; pending: boolean; view: OnboardingView
}) {
  if (pending) return <p role="status" className={banner.waiting}>Waiting for the box to apply your changes.</p>
  if (view.edit_mode === 'none') return <p role="alert" className={banner.waiting}>This onboarding no longer accepts edits. Your draft is kept here until you leave.</p>
  if (receipt && view.request_revision !== receipt.request_revision) return <p role="alert" className={banner.failed}>
    {view.notice || 'Your changes were not applied. The stored details changed.'} Your draft is kept. Reload current details before saving again.
  </p>
  return null
}

function isPending(receipt: { accepted: OnboardingAccepted; observedUpdate: number } | null, view: OnboardingView, updatedAt: number): boolean {
  if (!receipt) return false
  return updatedAt <= receipt.observedUpdate || view.pending.some(item => item.id === receipt.accepted.intent)
}

/** Keep the edit's base version and draft until the unit acknowledges its content.
 *  HTTP 202 only means queued; it is not a successful save. */
export function OnboardingEdit({ view, updatedAt, close }: { view: OnboardingView; updatedAt: number; close: () => void }) {
  const client = useQueryClient()
  const [base, setBase] = useState(view)
  const [generation, setGeneration] = useState(0)
  const [receipt, setReceipt] = useState<{ accepted: OnboardingAccepted; observedUpdate: number } | null>(null)
  const pending = isPending(receipt, view, updatedAt)
  const applied = receipt !== null && !pending && view.request_revision === receipt.accepted.request_revision
  useEffect(() => { if (applied) close() }, [applied, close])
  const queued = async (_project: string, accepted: OnboardingAccepted) => {
    const observedUpdate = client.getQueryState(onboardingKeys.detail(view.project))!.dataUpdatedAt
    setReceipt({ accepted, observedUpdate })
    await client.invalidateQueries({ queryKey: onboardingKeys.all })
  }
  const reload = () => {
    setBase(view)
    setReceipt(null)
    setGeneration(value => value + 1)
  }
  return <div className="space-y-4"><OnboardingForm key={generation} project={view.project} request={base.request} requestRevision={base.request_revision}
    repositoriesLocked={view.edit_mode !== 'repositories'} disabled={pending || view.edit_mode === 'none'} onSaved={queued}
    feedback={<EditFeedback receipt={receipt?.accepted ?? null} pending={pending} view={view} />} />
    <div className="flex flex-wrap justify-end gap-3"><span className="w-full text-right text-xs text-ink-muted">Reloading replaces your draft with the latest stored details.</span>
      <button type="button" className={button} disabled={pending} onClick={reload}>Reload current details</button>
      <button type="button" className={button} onClick={close}>Cancel editing</button></div></div>
}
