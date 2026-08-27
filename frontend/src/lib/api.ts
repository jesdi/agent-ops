import type { components } from './api-types'

export type BoardView = components['schemas']['BoardView']
export type TaskCard = components['schemas']['TaskCard']
export type CapacityView = components['schemas']['CapacityView']
export type TaskDetail = components['schemas']['TaskDetail']
export type BudgetView = components['schemas']['BudgetView']
export type FailuresView = components['schemas']['FailuresView']
export type QuarantineEntry = components['schemas']['QuarantineEntry']
export type FingerprintEntry = components['schemas']['FingerprintEntry']
export type HistoryView = components['schemas']['HistoryView']
export type EventEntry = components['schemas']['EventEntry']
export type SpecView = components['schemas']['SpecView']
export type PaneHistory = components['schemas']['PaneHistory']
export type NextClaimView = components['schemas']['NextClaimView']
export type GhostCard = components['schemas']['GhostCard']
export type TimelineEntry = components['schemas']['TimelineEntry']
export type IssueDescription = components['schemas']['IssueDescription']
export type MessageView = components['schemas']['MessageView']

export interface PendingIntent {
  action: string
  target: string
  issue: number
  actor: string
  created_at: string
}
export interface PendingIntentsView { intents: PendingIntent[] }
export interface QueueActionResult { ok: true; reason: string }
export interface IntentAccepted { status: 'pending'; intent: string }

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/**
 * FastAPI's `detail` is a string for HTTPException but an ARRAY of
 * validation-error objects for 422. `ApiError.detail` is rendered directly as
 * a React child, where an array of objects throws "Objects are not valid as a
 * React child" and blanks the page — so always normalise to a string here.
 */
export function formatDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        const msg = (item as { msg?: unknown } | null)?.msg
        return typeof msg === 'string' ? msg : JSON.stringify(item)
      })
      .join('; ')
  }
  return JSON.stringify(detail)
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = (await res.json()) as { detail?: unknown }
      if (body.detail !== undefined && body.detail !== null) {
        const formatted = formatDetail(body.detail)
        if (formatted !== '') detail = formatted
      }
    } catch { /* non-JSON error body */ }
    throw new ApiError(res.status, detail)
  }
  return res.json() as Promise<T>
}

const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body) })

export const api = {
  board: () => request<BoardView>('/board'),
  taskDetail: (target: string, issue: number) =>
    request<TaskDetail>(`/task/${target}/${issue}`),
  taskSpec: (target: string, issue: number) =>
    request<SpecView>(`/task/${target}/${issue}/spec`),
  taskHistory: (target: string, issue: number, lines = 2000) =>
    request<PaneHistory>(`/task/${target}/${issue}/history?lines=${lines}`),
  budget: () => request<BudgetView>('/budget'),
  failures: () => request<FailuresView>('/failures'),
  history: (limit = 200) => request<HistoryView>(`/history?limit=${limit}`),
  pendingIntents: () => request<PendingIntentsView>('/pending-intents'),
  // Queue actions — applied immediately, 200 or 422. Issue-only: the queue
  // (GitHub project board rank) has no per-target route.
  queueBoost: (issue: number, amount: number) =>
    post<QueueActionResult>('/queue/boost', { issue, amount }),
  queueNext: (issue: number, force: boolean) =>
    post<QueueActionResult>('/queue/next', { issue, force }),
  queueReady: (issue: number) => post<QueueActionResult>('/queue/ready', { issue }),
  // Intent actions — 202 accepted, applied by the next dispatcher pass.
  reply: (target: string, issue: number, text: string) =>
    post<IntentAccepted>(`/task/${target}/${issue}/reply`, { text }),
  park: (target: string, issue: number) =>
    post<IntentAccepted>(`/task/${target}/${issue}/park`, {}),
  kill: (target: string, issue: number) =>
    post<IntentAccepted>(`/task/${target}/${issue}/kill`, {}),
  // No _require_task server-side: a backlog card with no task file cancels too.
  cancel: (target: string, issue: number) =>
    post<IntentAccepted>(`/task/${target}/${issue}/cancel`, {}),
  retry: (target: string, issue: number) =>
    post<IntentAccepted>(`/task/${target}/${issue}/retry`, {}),
  resume: (target: string, issue: number, text?: string) =>
    post<IntentAccepted>(`/task/${target}/${issue}/resume`, text ? { text } : {}),
  taskDescription: (target: string, issue: number) =>
    request<IssueDescription>(`/task/${target}/${issue}/description`),
}
