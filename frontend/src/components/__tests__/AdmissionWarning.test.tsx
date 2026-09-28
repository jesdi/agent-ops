import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { AdmissionWarning } from '../AdmissionWarning'
import type { TaskCard } from '../../lib/api'

// Ticket 05: a stage's provider is fixed once it has a pick, so an operator
// choosing from the capacity-limited alternatives must never be offered a
// model of another provider than the one the requested (picked) model runs
// on — that would silently resume the wrong provider's session. The 422 on
// the HTTP routes is a backstop; this list is the actual UX.

type Admission = NonNullable<TaskCard['admission']>

function admission(overrides: Partial<Admission> = {}): Admission {
  return {
    requested: {
      model: 'anthropic/claude-opus-5', provider: 'anthropic',
      admitted: false, note: 'over pace',
    },
    any_provider: false,
    alternatives: [
      { model: 'anthropic/claude-sonnet-5', provider: 'anthropic',
        admitted: true, note: 'capacity available' },
      { model: 'openai/gpt-5-codex', provider: 'openai',
        admitted: true, note: 'capacity available' },
    ],
    ...overrides,
  }
}

function renderWarning(a: Admission) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return render(
    <AdmissionWarning target="widget" issue={42} admission={a} />, { wrapper })
}

it('the model picker offers only the requested model\'s provider, ' +
   'never a cross-provider alternative', async () => {
  renderWarning(admission())
  await userEvent.click(screen.getByRole('button', { name: /capacity limited/i }))

  expect(screen.getByText(/sonnet/i)).toBeInTheDocument()
  expect(screen.queryByText(/gpt-5-codex/i)).not.toBeInTheDocument()
})

it('offers every alternative when the requested model is not a fixed pick ' +
   '(no cross-provider alternatives configured)', async () => {
  // Same-provider-only alternatives is indistinguishable, from this
  // component's props alone, between "no pick yet" and "pick enforced" —
  // both cases legitimately show only same-provider choices here.
  renderWarning(admission({
    alternatives: [
      { model: 'anthropic/claude-sonnet-5', provider: 'anthropic',
        admitted: true, note: 'capacity available' },
    ],
  }))
  await userEvent.click(screen.getByRole('button', { name: /capacity limited/i }))

  expect(screen.getByText(/sonnet/i)).toBeInTheDocument()
})
