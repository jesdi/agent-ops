import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, type OnboardingAccepted, type OnboardingView } from '../lib/api'

export const onboardingKeys = {
  all: ['onboarding'] as const,
  detail: (project: string) => ['onboarding', project] as const,
}

/** Onboarding is driven by a separate unit, so task SSE cannot refresh it. */
export function useOnboardings() {
  return useQuery({ queryKey: onboardingKeys.all, queryFn: api.onboardings, refetchInterval: 2000 })
}

export function useOnboarding(project: string) {
  return useQuery({ queryKey: onboardingKeys.detail(project), queryFn: () => api.onboarding(project), refetchInterval: 2000, retry: false, enabled: project.length > 0 })
}

type Receipt = { accepted: OnboardingAccepted; observedUpdate: number }

function receiptPending(receipt: Receipt | null, view: OnboardingView | undefined, updatedAt: number): boolean {
  if (!receipt) return false
  return !view || updatedAt <= receipt.observedUpdate || view.pending.some(item => item.id === receipt.accepted.intent)
}

/** A successful POST is only queued. Keep its receipt across failed refreshes
 *  until a newer successful read shows that the unit consumed the intent. */
export function useOnboardingReceipt(view: OnboardingView | undefined, updatedAt: number) {
  const client = useQueryClient()
  const [receipt, setReceipt] = useState<Receipt | null>(null)
  const remember = async (accepted: OnboardingAccepted, project: string) => {
    const observedUpdate = client.getQueryState(onboardingKeys.detail(project))?.dataUpdatedAt ?? 0
    setReceipt({ accepted, observedUpdate })
    await client.invalidateQueries({ queryKey: onboardingKeys.all })
  }
  const pending = receiptPending(receipt, view, updatedAt)
  const applied = receipt !== null && !pending && view?.request_revision === receipt.accepted.request_revision
  return { accepted: receipt?.accepted ?? null, pending, applied,
    remember, clear: () => setReceipt(null) }
}
