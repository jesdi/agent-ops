import { expect, test } from '@playwright/test'

for (const width of [390, 1280]) {
  test(`artifacts remain accessible across sessions at ${width}px`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 })
    await request.post('/__control__/reset-messages')
    let reviewing = true
    let published = true
    let expired = false
    const spec = { id: 'spec', name: 'Specification', path: 'docs/spec.md', media_type: 'text/markdown',
      updated_at: '2026-09-13T00:00:00Z', stage: 'spec', status: 'published',
      url: '/api/task/widget/42/artifacts/spec', github_url: 'https://github.com/o/r/blob/task/42/docs/spec.md' }
    await page.route('**/api/task/widget/42/request', route => route.fulfill({ json: reviewing ? {
      kind: 'spec-approval', content: { kind: 'readable', path: 'docs/spec.md', media_type: 'text/markdown', text: '# Task artifacts\nKeep review material accessible across sessions.' },
    } : null }))
    await page.route('**/api/task/widget/42/artifacts', route => route.fulfill({ json: {
      items: [{ ...spec, github_url: published ? spec.github_url : '', status: published ? 'published' : 'local' },
        { ...spec, id: 'prototype', name: 'Prototype', media_type: 'text/html', github_url: '',
          status: expired ? 'expired' : 'local', url: expired ? '' : '/api/task/widget/42/artifacts/prototype' }],
      expired, expires_at: expired ? '2026-10-13T00:00:00Z' : '',
    } }))
    await page.context().route('**/api/task/widget/42/artifacts/prototype', route => route.fulfill({
      contentType: 'text/html', body: '<h1>Prototype preview</h1>',
    }))
    await page.goto('/task/widget/42')
    await expect(page.getByRole('link', { name: 'View spec on GitHub ↗' })).toBeVisible()
    const artifacts = page.getByRole('region', { name: 'Artifacts', exact: true })
    await expect(artifacts.getByRole('link', { name: 'Open Specification' })).toBeVisible()
    await page.screenshot({ path: `/tmp/task-artifacts-${width}.png`, fullPage: true })
    expect(await page.evaluate<number>('document.documentElement.scrollWidth')).toBeLessThanOrEqual(width)
    const opened = page.waitForEvent('popup')
    await artifacts.getByRole('link', { name: 'Open Prototype' }).click()
    const preview = await opened
    await expect(preview.getByRole('heading', { name: 'Prototype preview' })).toBeVisible()
    await preview.close()
    await page.getByRole('button', { name: 'approve spec', exact: true }).click()
    const approval = page.waitForRequest(req => req.url().endsWith('/reply') && req.method() === 'POST')
    await page.getByRole('button', { name: 'tap again to approve' }).click()
    expect((await approval).postDataJSON().text).toBe('Approved — proceed.')
    reviewing = false
    await page.reload()
    await expect(page.getByRole('button', { name: 'approve spec', exact: true })).toBeHidden()
    await expect(artifacts.getByRole('link', { name: 'Open Specification' })).toBeVisible()
    reviewing = true
    published = false
    await page.reload()
    await expect(page.getByText(/hasn’t been published/)).toBeVisible()
    await expect(page.getByRole('button', { name: 'approve spec', exact: true })).toBeEnabled()
    reviewing = false
    published = true
    expired = true
    await page.reload()
    await expect(artifacts.getByText('Expired', { exact: true })).toBeVisible()
    await expect(artifacts.getByRole('link', { name: 'Open Specification' })).toBeVisible()
    await expect(artifacts.getByRole('link', { name: 'Open Prototype' })).toHaveCount(0)
  })
}
