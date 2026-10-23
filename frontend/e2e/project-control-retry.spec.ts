import { expect, test } from '@playwright/test'

test('blocked control retries its requested state only after confirmation', async ({ page }) => {
  await page.route('**/api/projects', route => route.fulfill({ json: { projects: [{
    name: 'analytics', state: 'paused', repositories: ['example/analytics'],
    pending: { operation: 'ctl-1', requested: 'paused', phase: 'blocked',
      blocker: 'project changed since it was confirmed', pr: null },
  }] } }))
  const posts: unknown[] = []
  await page.route('**/api/projects/analytics/pause', route => {
    posts.push(route.request().postDataJSON())
    return route.fulfill({ json: { operation: 'ctl-1' } })
  })
  await page.goto('/')
  await page.getByRole('tab', { name: 'analytics' }).click()
  await expect(page.getByTestId('project-pending')).toContainText('blocked')
  await page.getByRole('button', { name: 'Retry pause', exact: true }).click()
  expect(posts).toEqual([])
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  expect(posts).toEqual([])
  await page.getByRole('button', { name: 'Retry pause', exact: true }).click()
  await page.getByRole('button', { name: 'Confirm pause', exact: true }).click()
  await expect.poll(() => posts).toEqual([{ confirm: true }])
})
