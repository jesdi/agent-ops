import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { AdmissionWarning } from '../AdmissionWarning'
import type { TaskCard } from '../../lib/api'

// The server supplies the eligible model choices.

type Admission = NonNullable<TaskCard['admission']>

function admission(overrides: Partial<Admission> = {}): Admission {
  return {
    requested: {
      model: 'anthropic/claude-opus-5', provider: 'anthropic',
      admitted: false, note: 'over pace',
    },
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

it('renders eligible alternatives from the server, including another provider', async () => {
  renderWarning(admission())
  await userEvent.click(screen.getByRole('button', { name: /capacity limited/i }))

  expect(screen.getByText(/sonnet/i)).toBeInTheDocument()
  expect(screen.getByText(/gpt-5-codex/i)).toBeInTheDocument()
})

it('renders only the alternatives supplied by the server', async () => {
  renderWarning(admission({
    alternatives: [
      { model: 'anthropic/claude-sonnet-5', provider: 'anthropic',
        admitted: true, note: 'capacity available' },
    ],
  }))
  await userEvent.click(screen.getByRole('button', { name: /capacity limited/i }))

  expect(screen.getByText(/sonnet/i)).toBeInTheDocument()
  expect(screen.queryByText(/gpt-5-codex/i)).not.toBeInTheDocument()
})
