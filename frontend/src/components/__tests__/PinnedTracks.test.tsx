import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { AdmissionWarning } from '../AdmissionWarning'
import { TaskCardView } from '../TaskCard'
import { UsagePanel } from '../UsagePanel'
import { useUsage } from '../../hooks/useResources'
import type { TaskCard } from '../../lib/api'
import { pinnedCard, pinnedWaitAdmission, unpinnedCard, usagePinned, usageUnpinned } from '../../test/fixtures'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'

// Spec pinned-tracks, ticket 03. Assertions are on visible text and roles.

async function expandedDetail(card: TaskCard) {
  renderWithProviders(<TaskCardView card={card} />)
  const chevron = screen.getByRole('button', { name: `Details for ${card.target}#${card.issue}` })
  await userEvent.click(chevron)
  return document.getElementById(chevron.getAttribute('aria-controls')!)!
}

it('a pinned task shows "pinned" and the track next to its model', async () => {
  const detail = await expandedDetail(pinnedCard)
  expect(detail).toHaveTextContent(/pinned/i)
  expect(detail).toHaveTextContent(/pinned[^a-z]*(to\s+)?frontend/i)
  expect(detail).toHaveTextContent('claude-fable-5-1')
})

it('a task whose launch is not pinned shows no pin', async () => {
  const detail = await expandedDetail(unpinnedCard)
  expect(detail).toHaveTextContent('openai/gpt-sol')
  expect(detail).not.toHaveTextContent(/pinned/i)
})

async function openWarning(admission: object) {
  renderWithProviders(
    <AdmissionWarning target="widget" issue={52} admission={admission as never} />)
  await userEvent.click(screen.getByRole('button', { name: /capacity limited/i }))
  return screen.getByRole('dialog', { name: /model capacity options/i })
}

it('a pinned wait says the task is pinned to the track', async () => {
  const dialog = await openWarning(pinnedWaitAdmission)
  expect(dialog).toHaveTextContent(/pinned to (the )?frontend/i)
})

it('a pinned wait offers every model of the policy, each with its verdict', async () => {
  const dialog = await openWarning(pinnedWaitAdmission)
  for (const [name, verdict] of [
    ['opus-5', /limited/], ['sonnet-5', /capacity available/],
    ['gpt-astra', /capacity available/], ['gpt-sol', /capacity available/],
  ] as const) {
    const choice = within(dialog).getByRole('button', { name: new RegExp(name) })
    expect(choice).toHaveTextContent(verdict)
  }
})

it('an unpinned wait says nothing about a pin', async () => {
  const dialog = await openWarning({ ...pinnedWaitAdmission, pinned_track: '',
    alternatives: pinnedWaitAdmission.alternatives.slice(1, 2) })
  expect(dialog).not.toHaveTextContent(/pinned/i)
  expect(within(dialog).queryByRole('button', { name: /gpt-astra/ })).toBeNull()
})

function Live() {
  const { data } = useUsage()
  return data ? <UsagePanel usage={data} /> : null
}

it('the usage panel names the pinned tracks under the priority control', async () => {
  server.use(http.get('/api/usage', () => HttpResponse.json(usagePinned)))
  renderWithProviders(<Live />)
  await screen.findByRole('radiogroup', { name: /priority/i })
  const line = await screen.findByText(/does not apply/i)
  expect(line).toHaveTextContent(/security.*architecture.*frontend/i)
})

it('with no pinned track the usage panel shows no such line', async () => {
  server.use(http.get('/api/usage', () => HttpResponse.json(usageUnpinned)))
  renderWithProviders(<Live />)
  await screen.findByRole('radiogroup', { name: /priority/i })
  await waitFor(() => expect(screen.queryByText(/does not apply/i)).toBeNull())
  expect(screen.queryByText(/pinned/i)).toBeNull()
})
