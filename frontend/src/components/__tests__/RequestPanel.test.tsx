import { screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { renderWithProviders } from '../../test/render'
import { RequestPanel } from '../RequestPanel'

function renderPanel() {
  return renderWithProviders(
    <RequestPanel target="widget" issue={42} busy={false} onApprove={() => {}} />,
  )
}

beforeEach(() => {
  server.use(...defaultHandlers)
})

// Cycle 1: HTML variant — sandboxed iframe
test('text/html readable request renders a sandboxed iframe with allow-scripts only', async () => {
  server.use(
    http.get('/api/task/widget/42/request', () =>
      HttpResponse.json({
        kind: 'answers',
        content: {
          kind: 'readable',
          media_type: 'text/html',
          path: 'ops/42/question.html',
          text: '<h1>Hello</h1><script>window.__ran=true</script>',
        },
      }),
    ),
  )
  renderPanel()
  const iframe = await screen.findByTitle('question.html')
  expect(iframe.tagName).toBe('IFRAME')
  expect(iframe).toHaveAttribute('sandbox', 'allow-scripts')
  // No allow-same-origin: that attribute value must not be present
  expect(iframe.getAttribute('sandbox')).not.toContain('allow-same-origin')
})

// Cycle 19a: unavailable spec-approval — recovery visible, no approve, panel stays
test('spec-approval with unavailable content shows recovery, no approve button', async () => {
  server.use(
    http.get('/api/task/widget/42/request', () =>
      HttpResponse.json({
        kind: 'spec-approval',
        content: {
          kind: 'unavailable',
          path: 'ops/42/spec.md',
          reason: 'file not found',
        },
      }),
    ),
  )
  renderPanel()
  const recovery = await screen.findByTestId('unavailable-recovery')
  expect(recovery).toBeInTheDocument()
  expect(within(recovery).getByText(/file not found/i)).toBeInTheDocument()
  expect(within(recovery).getByText(/ops\/42\/spec\.md/i)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument()
})

// Cycle 19b: transport error — ERROR state, not the unavailable presentation
test('/request 500 shows request-error, not unavailable-recovery', async () => {
  server.use(
    http.get('/api/task/widget/42/request', () =>
      HttpResponse.json({ detail: 'internal error' }, { status: 500 }),
    ),
  )
  renderPanel()
  expect(await screen.findByTestId('request-error')).toBeInTheDocument()
  expect(screen.queryByTestId('unavailable-recovery')).not.toBeInTheDocument()
})

// Cycle 2: downloadable-text variant — download affordance
test('non-markdown non-html readable request renders a download link with a data: URL', async () => {
  const text = 'col_a,col_b\n1,2'
  server.use(
    http.get('/api/task/widget/42/request', () =>
      HttpResponse.json({
        kind: 'answers',
        content: {
          kind: 'readable',
          media_type: 'text/plain',
          path: 'ops/42/data.txt',
          text,
        },
      }),
    ),
  )
  renderPanel()
  const link = await screen.findByRole('link', { name: /download data\.txt/i })
  expect(link).toHaveAttribute('download', 'data.txt')
  expect(link.getAttribute('href')).toContain('data:text/plain')
  expect(link.getAttribute('href')).toContain(encodeURIComponent(text))
})
