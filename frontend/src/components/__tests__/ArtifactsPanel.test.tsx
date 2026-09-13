import { screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'
import { ArtifactsPanel } from '../ArtifactsPanel'
import { RequestPanel } from '../RequestPanel'

const spec = {
  id: 'spec', name: 'Specification', path: 'docs/spec.md', media_type: 'text/markdown',
  updated_at: '2026-09-13T00:00:00Z', stage: 'spec', status: 'published',
  url: '/api/task/widget/42/artifacts/spec', github_url: 'https://github.com/o/r/blob/task/42/docs/spec.md',
}

function seed(items = [spec], expired = false) {
  server.use(http.get('/api/task/widget/42/artifacts', () =>
    HttpResponse.json({ items, expired, expires_at: '2026-10-13T00:00:00Z' })))
}

test('persistent artifacts open stable URLs in new tabs without an approval request', async () => {
  seed()
  renderWithProviders(<ArtifactsPanel target="widget" issue={42} />)
  const link = await screen.findByRole('link', { name: 'Open Specification' })
  expect(link).toHaveAttribute('href', spec.url)
  expect(link).toHaveAttribute('target', '_blank')
  expect(screen.getByText(/30 days after completion/)).toBeInTheDocument()
})

test('expired copies have no open link while GitHub artifacts remain', async () => {
  seed([spec, { ...spec, id: 'prototype', name: 'Prototype', status: 'expired', url: '', github_url: '' }], true)
  renderWithProviders(<ArtifactsPanel target="widget" issue={42} />)
  expect(await screen.findByRole('link', { name: 'Open Specification' })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Open Prototype' })).not.toBeInTheDocument()
  expect(screen.getByText('Expired')).toBeInTheDocument()
})

test('approval panel links to published spec beside the existing approval button', async () => {
  seed()
  server.use(http.get('/api/task/widget/42/request', () => HttpResponse.json({
    kind: 'spec-approval', content: { kind: 'readable', path: 'docs/spec.md', media_type: 'text/markdown', text: '# Design' },
  })))
  renderWithProviders(<RequestPanel target="widget" issue={42} busy={false} onApprove={() => {}} />)
  expect(await screen.findByRole('link', { name: 'View spec on GitHub ↗' })).toHaveAttribute('href', spec.url)
  expect(screen.getByRole('button', { name: 'approve spec' })).toBeInTheDocument()
})

test('publication failure preserves local review and approval', async () => {
  seed([{ ...spec, status: 'local', github_url: '' }])
  server.use(http.get('/api/task/widget/42/request', () => HttpResponse.json({
    kind: 'spec-approval', content: { kind: 'readable', path: 'docs/spec.md', media_type: 'text/markdown', text: '# Design' },
  })))
  renderWithProviders(<RequestPanel target="widget" issue={42} busy={false} onApprove={() => {}} />)
  expect(await screen.findByText(/hasn’t been published/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'approve spec' })).toBeEnabled()
  expect(screen.queryByRole('link', { name: 'View spec on GitHub ↗' })).not.toBeInTheDocument()
})

test.each([
  ['local', '', 'text/markdown', 'Not published · local copy'],
  ['local', '', 'text/html', 'Web preview'],
  ['unavailable', '', 'text/plain', 'Content unavailable'],
])('describes %s %s %s artifact availability', async (status, github_url, media_type, label) => {
  seed([{ ...spec, status, github_url, media_type, url: status === 'unavailable' ? '' : spec.url }])
  renderWithProviders(<ArtifactsPanel target="widget" issue={42} />)
  expect(await screen.findByText(`${label} · spec`)).toBeInTheDocument()
  if (status === 'unavailable') expect(screen.getByText('Unavailable')).toBeInTheDocument()
})

test('empty registry explains where review material will appear', async () => {
  seed([])
  renderWithProviders(<ArtifactsPanel target="widget" issue={42} />)
  expect(await screen.findByText(/No artifacts yet/)).toBeInTheDocument()
})

test('failed artifact query shows an error', async () => {
  server.use(http.get('/api/task/widget/42/artifacts', () => HttpResponse.json({ detail: 'registry offline' }, { status: 500 })))
  renderWithProviders(<ArtifactsPanel target="widget" issue={42} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('registry offline')
})

test('active task has latest-version guidance without cleanup date', async () => {
  server.use(http.get('/api/task/widget/42/artifacts', () => HttpResponse.json({ items: [spec], expired: false, expires_at: '' })))
  renderWithProviders(<ArtifactsPanel target="widget" issue={42} />)
  expect(await screen.findByText('Links open the latest version in a new tab.')).toBeInTheDocument()
})

test('failed artifact query preserves approval and local request content', async () => {
  server.use(
    http.get('/api/task/widget/42/artifacts', () => HttpResponse.json({ detail: 'registry offline' }, { status: 500 })),
    http.get('/api/task/widget/42/request', () => HttpResponse.json({
      kind: 'spec-approval', content: { kind: 'readable', path: 'docs/spec.md', media_type: 'text/markdown', text: '# Local design' },
    })),
  )
  renderWithProviders(<RequestPanel target="widget" issue={42} busy={false} onApprove={() => {}} />)
  expect(await screen.findByText(/Could not load the GitHub spec link/)).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: 'Local design' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'approve spec' })).toBeEnabled()
})
