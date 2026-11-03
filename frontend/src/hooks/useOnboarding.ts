import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'

export const onboardingKeys = {
  all: ['onboarding'] as const,
  detail: (project: string) => ['onboarding', project] as const,
}

/** Onboarding is driven by a separate unit, so task SSE cannot refresh it. */
export function useOnboardings() {
  return useQuery({ queryKey: onboardingKeys.all, queryFn: api.onboardings, refetchInterval: 2000 })
}

export function useOnboarding(project: string) {
  return useQuery({ queryKey: onboardingKeys.detail(project), queryFn: () => api.onboarding(project), refetchInterval: 2000, retry: false })
}
