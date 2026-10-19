import type { components } from './api-types'

export type ArtifactsView = components['schemas']['ArtifactsView']
export type BoardView = components['schemas']['BoardView']
export type BoardSnapshot = components['schemas']['BoardSnapshot']
export type TaskCard = components['schemas']['TaskCard']
export type Column = components['schemas']['Column']
export type Zone = Column['zone']
export type CapacityView = components['schemas']['CapacityView']
export type TaskDetail = components['schemas']['TaskDetail']
export type UsageView = components['schemas']['UsageView']
export type GateView = components['schemas']['GateView']
export type ProviderUsageView = components['schemas']['ProviderUsageView']
export type WindowView = components['schemas']['WindowView']
export type Severity = WindowView['severity']
export type WindowKind = WindowView['kind']
export type FailuresView = components['schemas']['FailuresView']
export type QuarantineEntry = components['schemas']['QuarantineEntry']
export type FingerprintEntry = components['schemas']['FingerprintEntry']
export type HistoryView = components['schemas']['HistoryView']
export type EventEntry = components['schemas']['EventEntry']
export type PaneHistory = components['schemas']['PaneHistory']
export type NextClaimView = components['schemas']['NextClaimView']
export type GhostCard = components['schemas']['GhostCard']
export type TimelineEntry = components['schemas']['TimelineEntry']
export type IssueDescription = components['schemas']['IssueDescription']
export type MessageView = components['schemas']['MessageView']
export type OperatorRequest = components['schemas']['OperatorRequest']
export type ReadableContent = components['schemas']['ReadableContent']
export type UnavailableContent = components['schemas']['UnavailableContent']
export type ModelSettingsView = components['schemas']['ModelSettingsView']
export type ModelsSetting = components['schemas']['ModelsSetting']
export type ModelsSave = components['schemas']['ModelsSave']
export type EntrySetting = components['schemas']['EntrySetting']
export type TrackSetting = components['schemas']['TrackSetting']

export interface PendingIntent {
  action: string
  target: string
  repo?: string
  issue: number
  actor: string
  created_at: string
  payload?: Record<string, unknown>
}
export interface PendingIntentsView { intents: PendingIntent[] }
export interface QueueActionResult { ok: true; reason: string }
export interface PrioritySet { ok: true; mode: string }
export interface IntentAccepted { status: 'pending'; intent: string }
export interface ResumeOptions { text?: string; model?: string; bypassUsage?: boolean }

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

/** `/task/<target>/<owner~name>/<issue>`: two repositories of one target may
 *  share an issue number. A task without a proven repository keeps the old path. */
export const taskPath = (target: string, issue: number | string, repo = '') =>
  repo ? `/task/${target}/${repo.replace('/', '~')}/${issue}` : `/task/${target}/${issue}`

const post = <T>(path: string, body: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body) })
const put = <T>(path: string, body: unknown) =>
  request<T>(path, { method: 'PUT', body: JSON.stringify(body) })

export const api = {
  boardSnapshot: () => request<components['schemas']['BoardSnapshot']>('/board/snapshot'),
  board: () => request<BoardView>('/board'),
  taskDetail: (target: string, issue: number, repo = '') =>
    request<TaskDetail>(`${taskPath(target, issue, repo)}`),
  taskHistory: (target: string, issue: number, repo = '', lines = 2000) =>
    request<PaneHistory>(`${taskPath(target, issue, repo)}/history?lines=${lines}`),
  usage: () => request<UsageView>('/usage'),
  failures: () => request<FailuresView>('/failures'),
  history: (limit = 200) => request<HistoryView>(`/history?limit=${limit}`),
  pendingIntents: () => request<PendingIntentsView>('/pending-intents'),
  // Queue actions — applied immediately, 200 or 422. They name the issue and
  // its repository ("owner/name"): the queue has no per-target route.
  queueBoost: (issue: number, amount: number, repo = '') =>
    post<QueueActionResult>('/queue/boost', { issue, amount, repo }),
  queueNext: (issue: number, force: boolean, repo = '') =>
    post<QueueActionResult>('/queue/next', { issue, force, repo }),
  queueReady: (issue: number, repo = '') =>
    post<QueueActionResult>('/queue/ready', { issue, repo }),
  // Priority mode — applied immediately, 200 or 422 with the reason.
  setPriority: (mode: string) => post<PrioritySet>('/priority', { mode }),
  // Model settings — stored immediately (200 or 422 with the reason); the
  // dispatcher reads them on its next pass.
  modelSettings: () => request<ModelSettingsView>('/settings/models'),
  // `revision` is the one the edited view carried: a stale one gets 409.
  saveModelSettings: (revision: string, models: ModelsSetting) =>
    put<ModelSettingsView>('/settings/models', { revision, models } satisfies ModelsSave),
  resetModelSettings: (revision: string) =>
    request<ModelSettingsView>(`/settings/models?revision=${encodeURIComponent(revision)}`, { method: 'DELETE' }),
  // Intent actions — 202 accepted, applied by the next dispatcher pass.
  reply: (target: string, issue: number, text: string, repo = '') =>
    post<IntentAccepted>(`${taskPath(target, issue, repo)}/reply`, { text }),
  // keepalive: a pagehide flush still reaches the server while the page unloads.
  answers: (target: string, issue: number, body: components['schemas']['AnswersReq'], repo = '') =>
    request<IntentAccepted>(`${taskPath(target, issue, repo)}/answers`,
      { method: 'POST', body: JSON.stringify(body), keepalive: true }),
  park: (target: string, issue: number, repo = '') =>
    post<IntentAccepted>(`${taskPath(target, issue, repo)}/park`, {}),
  kill: (target: string, issue: number, repo = '') =>
    post<IntentAccepted>(`${taskPath(target, issue, repo)}/kill`, {}),
  // No _require_task server-side: a backlog card with no task file cancels too.
  cancel: (target: string, issue: number, repo = '') =>
    post<IntentAccepted>(`${taskPath(target, issue, repo)}/cancel`, {}),
  retry: (target: string, issue: number, repo = '') =>
    post<IntentAccepted>(`${taskPath(target, issue, repo)}/retry`, {}),
  resume: (target: string, issue: number, options: ResumeOptions = {}, repo = '') =>
    post<IntentAccepted>(`${taskPath(target, issue, repo)}/resume`, {
      ...(options.text ? { text: options.text } : {}),
      ...(options.model ? { model: options.model } : {}),
      ...(options.bypassUsage ? { bypass_usage: true } : {}),
    }),
  forceRun: (target: string, issue: number, options: ResumeOptions = {}, repo = '') =>
    post<QueueActionResult>(`${taskPath(target, issue, repo)}/run`, {
      ...(options.model ? { model: options.model } : {}),
      ...(options.bypassUsage ? { bypass_usage: true } : {}),
    }),
  taskDescription: (target: string, issue: number, repo = '') =>
    request<IssueDescription>(`${taskPath(target, issue, repo)}/description`),
  taskArtifacts: (target: string, issue: number, repo = '') =>
    request<ArtifactsView>(`${taskPath(target, issue, repo)}/artifacts`),
  chooseSpecRepo: (target: string, issue: number, specRepo: string, repo = '') =>
    post<IntentAccepted>(`${taskPath(target, issue, repo)}/spec-repo`, { spec_repo: specRepo }),
  taskRequest: (target: string, issue: number, repo = '') =>
    request<OperatorRequest | null>(`${taskPath(target, issue, repo)}/request`),
}
