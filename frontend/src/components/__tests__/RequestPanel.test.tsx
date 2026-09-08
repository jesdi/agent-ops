import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { server } from '../../test/msw-server'
import { defaultHandlers } from '../../test/handlers'
import { renderWithProviders } from '../../test/render'
import { queryKeys } from '../../hooks/queryKeys'
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

// Cycle 21: same-path spec revision clears the armed approval confirmation
test('spec-approval revision at same path clears armed state', async () => {
  const user = userEvent.setup()
  const initialText = '# Spec v1\nInitial content.'
  const revisedText = '# Spec v2\nRevised content.'

  server.use(
    http.get('/api/task/widget/42/request', () =>
      HttpResponse.json({
        kind: 'spec-approval',
        content: {
          kind: 'readable',
          media_type: 'text/markdown',
          path: 'ops/42/spec.md',
          text: initialText,
        },
      }),
    ),
  )

  const { queryClient } = renderPanel()

  // Arm the approve button
  const approveBtn = await screen.findByRole('button', { name: /approve spec/i })
  await user.click(approveBtn)
  expect(screen.getByRole('button', { name: /tap again to approve/i })).toBeInTheDocument()

  // Update handler: same path, different text (revision)
  server.use(
    http.get('/api/task/widget/42/request', () =>
      HttpResponse.json({
        kind: 'spec-approval',
        content: {
          kind: 'readable',
          media_type: 'text/markdown',
          path: 'ops/42/spec.md',
          text: revisedText,
        },
      }),
    ),
  )

  // Trigger refetch
  await queryClient.invalidateQueries({ queryKey: queryKeys.request('widget', 42) })

  // Armed must reset — button shows "approve spec" again
  expect(await screen.findByRole('button', { name: /approve spec/i })).toBeInTheDocument()

  // A second tap re-arms rather than approving
  await user.click(screen.getByRole('button', { name: /approve spec/i }))
  expect(screen.getByRole('button', { name: /tap again to approve/i })).toBeInTheDocument()
})
