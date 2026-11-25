import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { server } from '../../test/msw-server'
import { renderWithProviders } from '../../test/render'
import { ReviewPage } from '../ReviewPage'

let delivery: object | null

beforeEach(() => {
  delivery = null
  server.use(
    http.get('/api/task/widget/42/request', () => HttpResponse.json({
      kind: 'plan-approval',
      content: { kind: 'readable', path: '.agent/review.html', media_type: 'text/html', text: '<p>page</p>' },
      revision: 'r1',
      answers: {},
    })),
    http.get('/api/task/widget/42/delivery', () => HttpResponse.json(delivery)),
    http.post('/api/task/widget/42/answers', () => HttpResponse.json({ status: 'pending', intent: 'x' }, { status: 202 })),
  )
})

test('a dropped approve takes back the approved notice', async () => {
  const { queryClient } = renderWithProviders(
    <Routes><Route path="/task/:repository/:issue/review" element={<ReviewPage />} /></Routes>,
    { route: '/task/widget/42/review' },
  )
  await screen.findByTestId('review-frame')
  fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
  fireEvent.click(screen.getByRole('button', { name: 'Tap again to approve' }))
  await waitFor(() => expect(screen.getByTestId('review-delivery')).toHaveTextContent('queued'))
  expect(screen.getByTestId('review-notice')).toHaveTextContent('approved')
  delivery = { state: 'dropped', reason: 'no open request', submit: 'approve', revision: 'r1' }
  await act(() => queryClient.invalidateQueries())
  await waitFor(() => expect(screen.getByTestId('review-delivery')).toHaveTextContent('dropped: no open request'))
  expect(screen.queryByTestId('review-notice')).not.toBeInTheDocument()
})
