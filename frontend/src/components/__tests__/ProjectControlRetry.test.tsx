import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'
import { ProjectTabs } from '../ProjectTabs'

it.each(['enabled', 'paused'] as const)('reconfirms a blocked %s request using its requested state', async (requested) => {
  const verb = requested === 'enabled' ? 'enable' : 'pause'
  const posts: unknown[] = []
  server.use(
    http.get('/api/projects', () => HttpResponse.json({ projects: [{
      name: 'analytics', state: requested, repositories: ['example/analytics'],
      pending: { operation: 'ctl-1', requested, phase: 'blocked', blocker: 'project changed', pr: null },
    }] })),
    http.post(`/api/projects/analytics/${verb}`, async ({ request }) => {
      posts.push(await request.json())
      return HttpResponse.json({ operation: 'ctl-1' })
    }),
  )
  renderWithProviders(<ProjectTabs selected="analytics" onSelect={() => {}} />)
  await userEvent.click(await screen.findByRole('button', { name: `Retry ${verb}` }))
  expect(posts).toEqual([])
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
  expect(posts).toEqual([])
  await userEvent.click(screen.getByRole('button', { name: `Retry ${verb}` }))
  await userEvent.click(screen.getByRole('button', { name: `Confirm ${verb}` }))
  await waitFor(() => expect(posts).toEqual([{ confirm: true }]))
})
