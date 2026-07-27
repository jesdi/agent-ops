import { render, screen } from '@testing-library/react'
import { BudgetBar } from '../BudgetBar'
import { budget, budgetUnavailable } from '../../test/fixtures'

it('shows utilization, reset countdown, and spawn verdict', () => {
  render(<BudgetBar budget={budget} />)
  expect(screen.getByText('62%')).toBeInTheDocument()
  expect(screen.getByText(/resets in 1h 35m/)).toBeInTheDocument()
  expect(screen.getByText(/will spawn/)).toBeInTheDocument()
})

it('the progressbar role carries a complete accessible range', () => {
  render(<BudgetBar budget={budget} />)
  const bar = screen.getByRole('progressbar')
  expect(bar).toHaveAttribute('aria-valuemin', '0')
  expect(bar).toHaveAttribute('aria-valuemax', '100')
  expect(bar).toHaveAttribute('aria-valuenow', '62')
  expect(bar).toHaveAccessibleName('usage budget utilization')
})

it('unavailable source states the consequence, not an empty gauge', () => {
  render(<BudgetBar budget={budgetUnavailable} />)
  expect(
    screen.getByText('usage unknown — dispatcher will not spawn'),
  ).toBeInTheDocument()
  expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
})
