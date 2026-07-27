import { render, screen } from '@testing-library/react'
import { BudgetBar } from '../BudgetBar'
import { budget, budgetUnavailable } from '../../test/fixtures'

it('shows utilization, reset countdown, and spawn verdict', () => {
  render(<BudgetBar budget={budget} />)
  expect(screen.getByText('62%')).toBeInTheDocument()
  expect(screen.getByText(/resets in 1h 35m/)).toBeInTheDocument()
  expect(screen.getByText(/will spawn/)).toBeInTheDocument()
})

it('unavailable source states the consequence, not an empty gauge', () => {
  render(<BudgetBar budget={budgetUnavailable} />)
  expect(
    screen.getByText('usage unknown — dispatcher will not spawn'),
  ).toBeInTheDocument()
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})
