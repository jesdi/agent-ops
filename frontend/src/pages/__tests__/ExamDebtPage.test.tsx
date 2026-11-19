import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { AppShell } from '../../components/AppShell'
import { renderWithProviders } from '../../test/render'
import { server } from '../../test/msw-server'
import { ExamDebtPage } from '../ExamDebtPage'

const SHA = 'a'.repeat(40)
const BOX = 'https://box.tailnet.ts.net'
const deferred = { repo: 'jesdi/general-skills', pr: 240, title: 'Lease the base SHA', url: `${BOX}/exam/jesdi/general-skills/240`, head_sha: SHA, state: 'open', debt: true }
const merged = { repo: 'jesdi/claude-config', pr: 12, title: 'Pin the hook order', url: `${BOX}/exam/jesdi/claude-config/12`, head_sha: 'b'.repeat(40), state: 'grading', debt: true }
const notDebt = { repo: 'jesdi/other', pr: 7, title: 'Not owed anything', url: `${BOX}/exam/jesdi/other/7`, head_sha: 'c'.repeat(40), state: 'open', debt: false }

const serve = (exams: unknown[]) => server.use(http.get('/api/exams', () => HttpResponse.json({ exams })))
const hrefOf = (el: HTMLElement) => el.closest('a')?.getAttribute('href')

describe('exam debt page', () => {
  it('lists a deferred exam and a merged one with repo, PR number, title and state, each linking to its exam page', async () => {
    serve([deferred, merged])
    renderWithProviders(<ExamDebtPage />)
    const first = (await screen.findByText(deferred.title)).closest('li, tr, article') as HTMLElement
    const second = screen.getByText(merged.title).closest('li, tr, article') as HTMLElement
    expect(first).not.toBeNull()
    expect(within(first).getByText(/jesdi\/general-skills/)).toBeInTheDocument()
    expect(within(first).getByText(/240/)).toBeInTheDocument()
    expect(within(first).getByText(/open/i)).toBeInTheDocument()
    expect(within(second).getByText(/jesdi\/claude-config/)).toBeInTheDocument()
    expect(within(second).getByText(/12/)).toBeInTheDocument()
    expect(within(second).getByText(/grading/i)).toBeInTheDocument()
    expect(hrefOf(screen.getByText(deferred.title))).toBe('/exam/jesdi/general-skills/240')
    expect(hrefOf(screen.getByText(merged.title))).toBe('/exam/jesdi/claude-config/12')
  })

  it('does not list an exam that is not debt', async () => {
    serve([deferred, notDebt])
    renderWithProviders(<ExamDebtPage />)
    expect(await screen.findByText(deferred.title)).toBeInTheDocument()
    expect(screen.queryByText(notDebt.title)).not.toBeInTheDocument()
  })

  it('says there is no debt when the list is empty', async () => {
    serve([])
    renderWithProviders(<ExamDebtPage />)
    expect(await screen.findByText(/no exam debt/i)).toBeInTheDocument()
  })

  it('drops a debt exam from the list, without a reload, once it is passed', async () => {
    let exams = [deferred, merged]
    server.use(http.get('/api/exams', () => HttpResponse.json({ exams })))
    // No live connection: the page polls on its own.
    renderWithProviders(<ExamDebtPage />, { connected: false })
    expect(await screen.findByText(deferred.title)).toBeInTheDocument()
    exams = [merged]
    await waitFor(() => expect(screen.queryByText(deferred.title)).not.toBeInTheDocument(), { timeout: 9000 })
    expect(screen.getByText(merged.title)).toBeInTheDocument()
  }, 12000)
})

describe('nav', () => {
  it('shows the exam debt page with the number of debt exams', async () => {
    serve([deferred, merged, notDebt])
    renderWithProviders(<AppShell><p>body</p></AppShell>)
    const link = await screen.findByRole('link', { name: /exam debt/i })
    expect(link).toHaveAttribute('href', '/exam-debt')
    await waitFor(() => expect(link).toHaveTextContent('2'))
  })
})
