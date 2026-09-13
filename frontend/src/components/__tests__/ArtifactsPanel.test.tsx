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
