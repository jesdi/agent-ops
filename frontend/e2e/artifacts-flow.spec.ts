import { expect, test } from '@playwright/test'

for (const width of [390, 1280]) {
  test(`artifacts remain accessible across sessions at ${width}px`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 })
    await request.post('/__control__/reset')
    let reviewing = true
    let published = true
    let expired = false
    const spec = { id: 'spec', name: 'Specification', path: 'docs/spec.md', media_type: 'text/markdown',
      updated_at: '2026-09-13T00:00:00Z', stage: 'spec', status: 'published',
      url: '/api/task/widget/42/artifacts/spec', github_url: 'https://github.com/o/r/blob/task/42/docs/spec.md' }
    await page.route('**/api/task/widget/42/request', route => route.fulfill({ json: reviewing ? {
      kind: 'plan-approval', content: { kind: 'readable', path: '.agent/review.html', media_type: 'text/markdown', text: '# Task artifacts\nKeep review material accessible across sessions.' },
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
    await expect(page.getByRole('link', { name: 'open review' })).toHaveAttribute('href', '/task/widget/42/review')
    reviewing = false
    await page.reload()
    await expect(page.getByRole('link', { name: 'open review' })).toBeHidden()
    await expect(artifacts.getByRole('link', { name: 'Open Specification' })).toBeVisible()
    reviewing = true
    published = false
    await page.reload()
    await expect(page.getByText(/hasn’t been published/)).toBeVisible()
    await expect(page.getByRole('link', { name: 'open review' })).toBeVisible()
    reviewing = false
    published = true
    expired = true
    await page.reload()
    await expect(artifacts.getByText('Expired', { exact: true })).toBeVisible()
    await expect(artifacts.getByRole('link', { name: 'Open Specification' })).toBeVisible()
    await expect(artifacts.getByRole('link', { name: 'Open Prototype' })).toHaveCount(0)
  })
}

// #281: the review page registered as an artifact opened the raw HTML, which
// has no Send control; it must open the console's review route instead.
for (const [kind, path, send] of [['plan-approval', '.agent/review.html', 'Send changes'],
  ['answers', '.agent/questionnaire.html', 'Send answers']] as const) {
  test(`the artifact link of the open ${kind} page opens the review route with ${send}`, async ({ page }) => {
    await page.route('**/api/task/widget/42/request', route => route.fulfill({ json: {
      kind, revision: 'r1', answers: {},
      content: { kind: 'readable', path, media_type: 'text/html', text: '<p>pick one</p>' },
    } }))
    await page.route('**/api/task/widget/42/artifacts', route => route.fulfill({ json: {
      items: [{ id: 'review', name: 'Review page', path, media_type: 'text/html',
        updated_at: '2026-11-24T00:00:00Z', stage: 'plan', status: 'local',
        url: '/api/task/widget/42/artifacts/review', github_url: '' }],
      expired: false, expires_at: '',
    } }))
    await page.goto('/task/widget/42')
    const link = page.getByRole('region', { name: 'Artifacts', exact: true }).getByRole('link', { name: 'Open Review page' })
    await expect(link).toHaveAttribute('href', '/task/widget/42/review')
    await link.click()
    await expect(page.getByRole('button', { name: send })).toBeVisible()
  })
}

// #281: only the open request's page goes to the review route; every other
// artifact, and the same page with no open request, keeps its raw preview.
test('other artifacts and a closed request keep the raw preview in a new tab', async ({ page }) => {
  let request: 'open' | 'closed' | 'failing' = 'open'
  await page.route('**/api/task/widget/42/request', route => request === 'failing'
    ? route.fulfill({ status: 500, json: { detail: 'request offline' } })
    : route.fulfill({ json: request === 'open' ? {
      kind: 'plan-approval', revision: 'r1', answers: {},
      content: { kind: 'readable', path: '.agent/review.html', media_type: 'text/html', text: '<p>pick one</p>' },
    } : null }))
  const artifact = { media_type: 'text/html', updated_at: '2026-11-24T00:00:00Z', stage: 'plan', status: 'local', github_url: '' }
  await page.route('**/api/task/widget/42/artifacts', route => route.fulfill({ json: {
    items: [{ ...artifact, id: 'review', name: 'Review page', path: '.agent/review.html', url: '/api/task/widget/42/artifacts/review' },
      { ...artifact, id: 'prototype', name: 'Prototype', path: '.agent/prototype.html', url: '/api/task/widget/42/artifacts/prototype' }],
    expired: false, expires_at: '',
  } }))
  await page.context().route('**/api/task/widget/42/artifacts/prototype', route => route.fulfill({
    contentType: 'text/html', body: '<h1>Prototype preview</h1>',
  }))
  await page.goto('/task/widget/42')
  const artifacts = page.getByRole('region', { name: 'Artifacts', exact: true })
  const review = artifacts.getByRole('link', { name: 'Open Review page' })
  const prototype = artifacts.getByRole('link', { name: 'Open Prototype' })
  await expect(review).toHaveAttribute('href', '/task/widget/42/review')
  await expect(prototype).toHaveAttribute('href', '/api/task/widget/42/artifacts/prototype')
  await expect(prototype).toHaveAttribute('target', '_blank')
  const opened = page.waitForEvent('popup')
  await prototype.click()
  await expect((await opened).getByRole('heading', { name: 'Prototype preview' })).toBeVisible()
  for (const state of ['closed', 'failing'] as const) {
    request = state
    await page.reload()
    await expect(review).toHaveAttribute('href', '/api/task/widget/42/artifacts/review')
    await expect(review).toHaveAttribute('target', '_blank')
  }
})
