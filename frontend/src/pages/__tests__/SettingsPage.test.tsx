import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import type { EntrySetting, ModelSettingsView, ModelsSave, ModelsSetting } from '../../lib/api'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'
import { SettingsPage } from '../SettingsPage'

const entry = (provider: string, model: string, effort = ''): EntrySetting => ({ provider, model, effort })
const OPUS = entry('anthropic', 'claude-opus-5')

const VIEW: ModelSettingsView = {
  models: {
    triage: [entry('anthropic', 'claude-sonnet-5', 'medium')],
    untracked: 'standard',
    tracks: {
      standard: {
        when: 'Any task.', plan_review: true, spec: [OPUS], plan: [OPUS],
        implement: [entry('anthropic', 'claude-sonnet-5', 'high'), entry('openai', 'gpt-luna')],
        review: [OPUS],
      },
    },
    pinned: [],
    review_second: null,
  },
  revision: '', source: 'targets.yaml', set_by: '', set_at: '', error: '',
  efforts: { anthropic: ['low', 'medium', 'high', 'xhigh', 'max'], openai: ['minimal', 'low', 'medium', 'high', 'xhigh'] },
  default_provider: 'anthropic',
  known_models: { anthropic: ['claude-opus-5', 'claude-sonnet-5'], openai: ['gpt-luna'] },
  project_overrides: [],
}
const STORED: ModelSettingsView = { ...VIEW, revision: 'r1', source: 'console', set_by: 'jesdi', set_at: new Date().toISOString() }

/** Serves VIEW (or `initial`) and records each saved block, answering as
 *  stored; `revisions` records the revision each save was sent from. */
function serve(initial: ModelSettingsView = VIEW) {
  const bodies: ModelsSetting[] = []
  const revisions: string[] = []
  server.use(
    http.get('/api/settings/models', () => HttpResponse.json(initial)),
    http.put('/api/settings/models', async ({ request }) => {
      const { revision, models } = (await request.json()) as ModelsSave
      bodies.push(models)
      revisions.push(revision)
      return HttpResponse.json({ ...STORED, revision: `r${revisions.length + 1}`, models })
    }),
  )
  return Object.assign(bodies, { revisions })
}

it('saves an edited stage list and starts the form over from the stored block', async () => {
  const bodies = serve()
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  const save = await screen.findByRole('button', { name: 'Save' })
  expect(save).toBeDisabled()

  await user.click(screen.getByRole('button', { name: 'Move standard implement entry 2 up' }))
  await user.selectOptions(screen.getByLabelText('standard implement entry 1 effort'), 'high')
  await user.click(save)

  await screen.findByText('Saved. The next dispatcher pass uses it.')
  expect(bodies[0]?.tracks.standard?.implement).toEqual([
    entry('openai', 'gpt-luna', 'high'), entry('anthropic', 'claude-sonnet-5', 'high')])
  expect(bodies[0]?.review_second).toBeNull()
  expect(screen.getByTestId('settings-source')).toHaveTextContent('set in the console')
  expect(save).toBeDisabled()

  // The next save is sent from the revision the first one returned.
  await user.type(screen.getByLabelText('When standard'), ' More.')
  await user.click(save)
  await waitFor(() => expect(bodies.revisions).toEqual(['', 'r2']))
})

it('locks the form while a save waits, so no edit is lost', async () => {
  serve()
  let answer: () => void = () => {}
  server.use(http.put('/api/settings/models', async ({ request }) => {
    const { models } = (await request.json()) as ModelsSave
    await new Promise<void>((resolve) => { answer = resolve })
    return HttpResponse.json({ ...STORED, models })
  }))
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  const when = await screen.findByLabelText('When standard')
  await user.type(when, ' More.')
  await user.click(screen.getByRole('button', { name: 'Save' }))

  await waitFor(() => expect(when).toBeDisabled())
  expect(screen.getByRole('button', { name: 'Add track' })).toBeDisabled()
  answer()
  await waitFor(() => expect(screen.getByLabelText('When standard')).toBeEnabled())
  expect(screen.getByLabelText('When standard')).toHaveValue('Any task. More.')
})

it('adds a second review model on another provider, with no effort', async () => {
  const bodies = serve()
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  const general = within(await screen.findByRole('region', { name: 'General' }))
  await user.click(general.getAllByRole('button', { name: '+ Add model' })[1]!)
  await user.type(screen.getByLabelText('Second review entry 1 model'), 'gpt-sol')
  expect(screen.queryByLabelText('Second review entry 1 effort')).toBeNull()
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(bodies[0]?.review_second).toEqual(entry('openai', 'gpt-sol')))
})

it('shows why the server refused a block', async () => {
  serve()
  server.use(http.put('/api/settings/models', () =>
    HttpResponse.json({ detail: 'unfinished tasks run on a track this removes: alpha#9 (standard)' }, { status: 422 })))
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('When standard'), ' More.')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('alpha#9 (standard)'))
})

it('keeps untracked and pinned on a renamed track, and drops a removed one from pinned', async () => {
  const bodies = serve(STORED)
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

  await waitFor(() => expect(bodies).toHaveLength(1))
  expect(Object.keys(bodies[0]!.tracks)).toEqual(['base'])
  expect(bodies[0]!.untracked).toBe('base')
  expect(bodies[0]!.pinned).toEqual(['base'])
})

it('refuses to save two tracks of one name', async () => {
  serve()
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Add track' }))
  const second = screen.getAllByLabelText('Track name')[1]!
  await user.clear(second)
  await user.type(second, 'standard')

  expect(screen.getAllByText('Another track has this name.')).toHaveLength(2)
  expect(second).toHaveAttribute('aria-invalid', 'true')
  expect(screen.getByRole('status')).toHaveTextContent('Fix the track names to save.')
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  // Only the first track is the untracked one, before and after the clash.
  expect(screen.getAllByText('untracked')).toHaveLength(1)
  await user.type(second, '-2')
  expect(screen.getByLabelText('Untracked track')).toHaveDisplayValue('standard')
  expect(within(screen.getByRole('region', { name: 'Track standard' })).getByText('untracked')).toBeInTheDocument()
})

it('names a new track with a name no track has', async () => {
  serve({ ...VIEW, models: { ...VIEW.models, tracks: { ...VIEW.models.tracks, 'track-2': VIEW.models.tracks.standard! } } })
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Add track' }))
  expect(screen.getAllByLabelText('Track name').map((i) => (i as HTMLInputElement).value))
    .toEqual(['standard', 'track-2', 'track-3'])
})

it('resets to targets.yaml only after a confirm', async () => {
  let resets = 0
  serve(STORED)
  server.use(http.delete('/api/settings/models', ({ request }) => {
    expect(new URL(request.url).searchParams.get('revision')).toBe('r1')
    resets += 1
    return HttpResponse.json(VIEW)
  }))
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Reset to targets.yaml' }))
  expect(resets).toBe(0)
  await user.click(screen.getByRole('button', { name: 'Confirm reset' }))
  await waitFor(() => expect(screen.getByTestId('settings-source')).toHaveTextContent('from targets.yaml'))
  expect(resets).toBe(1)
})

it('discards unsaved changes', async () => {
  serve()
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  const when = await screen.findByLabelText('When standard')
  await user.type(when, ' More.')
  await user.click(screen.getByRole('button', { name: 'Discard changes' }))
  expect(screen.getByLabelText('When standard')).toHaveValue('Any task.')
  expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
})

it('drops an effort the new provider does not have, and keeps one it has', async () => {
  serve()
  renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  const effort = await screen.findByLabelText('Triage entry 1 effort')
  await user.selectOptions(screen.getByLabelText('Triage entry 1 provider'), 'openai')
  expect(effort).toHaveValue('medium')
  await user.selectOptions(effort, 'minimal')
  await user.selectOptions(screen.getByLabelText('Triage entry 1 provider'), 'anthropic')
  expect(effort).toHaveValue('')
})


it('keeps edits on the view they were made on when a newer one arrives', async () => {
  serve(STORED)
  const { queryClient } = renderWithProviders(<SettingsPage />)
  const user = userEvent.setup()
  await user.type(await screen.findByLabelText('When standard'), ' Mine.')

  // Another operator saves; this tab refetches in the background.
  const theirs = { ...STORED, revision: 'r9', models: { ...STORED.models, pinned: ['standard'] } }
  const sent: string[] = []
  server.use(
    http.get('/api/settings/models', () => HttpResponse.json(theirs)),
    http.put('/api/settings/models', async ({ request }) => {
      sent.push(((await request.json()) as ModelsSave).revision)
      return HttpResponse.json({ detail: 'the model settings changed since this page loaded them' }, { status: 409 })
    }),
  )
  await queryClient.invalidateQueries()

  await screen.findByText(/Someone saved newer model settings/)
  expect(screen.getByLabelText('When standard')).toHaveValue('Any task. Mine.')
  await user.click(screen.getByRole('button', { name: 'Save' }))
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('changed since this page loaded'))
  expect(sent).toEqual(['r1'])   // the revision the edits were made on, not r9

  await user.click(screen.getByRole('button', { name: 'Discard changes' }))
  expect(screen.getByLabelText('When standard')).toHaveValue('Any task.')
  expect(screen.getByText('1. standard')).toBeInTheDocument()
  expect(screen.getByRole('status')).toHaveTextContent('')
})

it('follows a newer view at once when there is nothing to lose', async () => {
  serve(STORED)
  const { queryClient } = renderWithProviders(<SettingsPage />)
  await screen.findByLabelText('When standard')
  server.use(http.get('/api/settings/models', () =>
    HttpResponse.json({ ...STORED, revision: 'r9', models: { ...STORED.models, pinned: ['standard'] } })))
  await queryClient.invalidateQueries()
  await screen.findByText('1. standard')
  expect(screen.queryByText(/Someone saved newer model settings/)).toBeNull()
})
