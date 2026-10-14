import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import type { ModelSettingsView, ModelsSetting } from '../../lib/api'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'
import { SettingsPage } from '../SettingsPage'

const VIEW: ModelSettingsView = {
  models: {
    triage: ['anthropic/claude-sonnet-5@medium'],
    untracked: 'standard',
    pinned: [],
    tracks: {
      standard: {
        when: 'Any task.', plan_review: true,
        spec: ['anthropic/claude-opus-5'], plan: ['anthropic/claude-opus-5'],
        implement: ['anthropic/claude-sonnet-5@high', 'openai/gpt-luna'],
        review: ['anthropic/claude-opus-5'],
      },
    },
  },
  source: 'targets.yaml', set_by: '', set_at: '', error: '',
  efforts: { anthropic: ['low', 'medium', 'high', 'xhigh', 'max'], openai: ['minimal', 'low', 'medium', 'high', 'xhigh'] },
  known_models: ['anthropic/claude-opus-5', 'openai/gpt-luna'],
  target_overrides: [],
}

it('saves an edited stage list in the targets.yaml shape', async () => {
  let body: ModelsSetting | undefined
  server.use(
    http.get('/api/settings/models', () => HttpResponse.json(VIEW)),
    http.put('/api/settings/models', async ({ request }) => {
      body = (await request.json()) as ModelsSetting
      return HttpResponse.json({ ...VIEW, models: body, source: 'console', set_by: 'jesdi', set_at: new Date().toISOString() })
    }),
  )
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  const save = await screen.findByRole('button', { name: 'Save' })
  expect(save).toBeDisabled()

  await user.click(screen.getByRole('button', { name: 'Move standard implement entry 2 up' }))
  await user.selectOptions(screen.getByLabelText('standard implement entry 1 effort'), 'high')
  await user.click(save)

  await screen.findByText('Saved. The next dispatcher pass uses it.')
  expect(body?.tracks.standard?.implement).toEqual(['openai/gpt-luna@high', 'anthropic/claude-sonnet-5@high'])
  expect(body).not.toHaveProperty('review_second')
  expect(screen.getByTestId('settings-source')).toHaveTextContent('set in the console')
})

it('shows why the server refused a block', async () => {
  server.use(
    http.get('/api/settings/models', () => HttpResponse.json(VIEW)),
    http.put('/api/settings/models', () =>
      HttpResponse.json({ detail: 'unfinished tasks run on a track this removes: alpha#9 (standard)' }, { status: 422 })),
  )
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('When standard'), ' More.')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('alpha#9 (standard)'))
})

it('keeps untracked and pinned on a renamed track, and drops a removed one from pinned', async () => {
  let body: ModelsSetting | undefined
  server.use(
    http.get('/api/settings/models', () => HttpResponse.json({ ...VIEW, source: 'console', set_by: 'jesdi', set_at: new Date().toISOString() })),
    http.put('/api/settings/models', async ({ request }) => {
      body = (await request.json()) as ModelsSetting
      return HttpResponse.json({ ...VIEW, models: body })
    }),
  )
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Add track' }))
  await user.selectOptions(screen.getByLabelText('Pin a track'), 'track-2')
  await user.selectOptions(screen.getByLabelText('Pin a track'), 'standard')
  const [standardName] = screen.getAllByLabelText('Track name')
  await user.clear(standardName!)
  await user.type(standardName!, 'base')
  await user.click(within(screen.getByRole('region', { name: 'Track track-2' }))
    .getByRole('button', { name: 'Remove track' }))
  await user.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(body).toBeDefined())
  expect(Object.keys(body!.tracks)).toEqual(['base'])
  expect(body!.untracked).toBe('base')
  expect(body!.pinned).toEqual(['base'])
})

it('resets to targets.yaml only after a confirm', async () => {
  let resets = 0
  server.use(
    http.get('/api/settings/models', () => HttpResponse.json({ ...VIEW, source: 'console', set_by: 'jesdi', set_at: new Date().toISOString() })),
    http.delete('/api/settings/models', () => { resets += 1; return HttpResponse.json(VIEW) }),
  )
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Reset to targets.yaml' }))
  expect(resets).toBe(0)
  await user.click(screen.getByRole('button', { name: 'Confirm reset' }))
  await waitFor(() => expect(screen.getByTestId('settings-source')).toHaveTextContent('from targets.yaml'))
  expect(resets).toBe(1)
})
