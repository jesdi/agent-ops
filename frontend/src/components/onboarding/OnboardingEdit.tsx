import { useEffect, useState } from 'react'
import type { OnboardingAccepted, OnboardingView } from '../../lib/api'
import { useOnboardingReceipt } from '../../hooks/useOnboarding'
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

/** Keep the edit's base version and draft until the unit acknowledges its content.
 *  HTTP 202 only means queued; it is not a successful save. */
export function OnboardingEdit({ view, updatedAt, close }: { view: OnboardingView; updatedAt: number; close: () => void }) {
  const [base, setBase] = useState(view)
  const [generation, setGeneration] = useState(0)
  const receipt = useOnboardingReceipt(view, updatedAt)
  useEffect(() => { if (receipt.applied) close() }, [receipt.applied, close])
  const reload = () => {
    setBase(view)
    receipt.clear()
    setGeneration(value => value + 1)
  }
  const resolved = base.steps.some(step => step.name === 'resolve' && step.state === 'verified')
  return <div className="space-y-4"><OnboardingForm key={generation} project={view.project} request={base.request} requestRevision={base.request_revision} proposal={resolved ? base.proposal : null} inspection={resolved ? base.inspection : undefined}
    repositoriesLocked={view.edit_mode !== 'repositories'} disabled={receipt.pending || view.edit_mode === 'none'} onSaved={(_project, accepted) => receipt.remember(accepted, view.project)}
    feedback={<EditFeedback receipt={receipt.accepted} pending={receipt.pending} view={view} />} />
    <div className="flex flex-wrap justify-end gap-3"><span className="w-full text-right text-xs text-ink-muted">Reloading replaces your draft with the latest stored details.</span>
      <button type="button" className={button} disabled={receipt.pending} onClick={reload}>Reload current details</button>
      <button type="button" className={button} onClick={close}>Cancel editing</button></div></div>
}
