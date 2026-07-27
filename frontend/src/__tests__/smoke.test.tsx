import { render, screen } from '@testing-library/react'

test('harness renders', () => {
  render(<div>agent-ops console</div>)
  expect(screen.getByText('agent-ops console')).toBeInTheDocument()
})
