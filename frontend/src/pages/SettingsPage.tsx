import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { queryKeys } from '../hooks/queryKeys'
import { api, ApiError, type ModelSettingsView, type ModelsSetting } from '../lib/api'
import { relativeTime } from '../lib/format'
import { banner, chip } from '../lib/tone'

const STAGES = ['spec', 'plan', 'implement', 'review'] as const
type Stage = (typeof STAGES)[number]

interface TrackForm extends Record<Stage, string[]> {
  id: number            // React key: survives a rename
  name: string
  when: string
  plan_review: boolean
}
interface Form {
  triage: string[]
  untracked: string
  pinned: string[]
  review_second: string
  tracks: TrackForm[]
}
interface Entry { provider: string; model: string; effort: string }

let nextId = 0

/** `provider/model[@effort]`; a bare model is anthropic's (models.parse_entry). */
function parseEntry(text: string): Entry {
  const at = text.indexOf('@')
  const id = at < 0 ? text : text.slice(0, at)
  const effort = at < 0 ? '' : text.slice(at + 1)
  const slash = id.indexOf('/')
  return slash < 0
    ? { provider: 'anthropic', model: id, effort }
    : { provider: id.slice(0, slash), model: id.slice(slash + 1), effort }
}

function formatEntry({ provider, model, effort }: Entry): string {
  return `${provider}/${model}${effort ? `@${effort}` : ''}`
}

function toForm(models: ModelsSetting): Form {
  return {
    triage: models.triage,
    untracked: models.untracked,
    pinned: models.pinned,
    review_second: models.review_second ?? '',
    tracks: Object.entries(models.tracks).map(([name, t]) => ({
      id: nextId++, name, when: t.when, plan_review: t.plan_review ?? true,
      spec: t.spec, plan: t.plan, implement: t.implement, review: t.review,
    })),
  }
}

function toBody(form: Form): ModelsSetting {
  return {
    triage: form.triage,
    untracked: form.untracked,
    pinned: form.pinned,
    ...(form.review_second.trim() ? { review_second: form.review_second.trim() } : {}),
    tracks: Object.fromEntries(form.tracks.map(({ id: _id, name, ...t }) => [name, t])),
  }
}

function move<T>(list: T[], from: number, to: number): T[] {
  if (to < 0 || to >= list.length) return list
  const out = [...list]
  out.splice(to, 0, ...out.splice(from, 1))
  return out
}

const BUTTON = 'rounded border border-border px-3 py-1.5 text-sm disabled:opacity-50'
const PRIMARY = 'rounded bg-ink px-3 py-1.5 text-sm text-surface-raised disabled:opacity-50'
const ICON = 'rounded border border-border px-2 py-1 text-xs leading-none disabled:opacity-30'
const FIELD = 'rounded border border-border bg-surface-raised px-2 py-1 text-sm'
const SECTION = 'rounded border border-border bg-surface-raised p-3 sm:p-4'

export function SettingsPage() {
  const query = useQuery({ queryKey: queryKeys.modelSettings, queryFn: api.modelSettings })
  // A save, a reset or a discard starts the form over from the server's view.
  const [revision, setRevision] = useState(0)
  // Outlives the editor, which a save remounts with the stored block.
  const [saved, setSaved] = useState(false)
  if (query.isPending) return <p className="p-4 text-ink-muted">loading model settings…</p>
  if (query.isError) {
    return <p className="p-4 text-failed-fg">model settings unavailable: {query.error.message}</p>
  }
  return <SettingsEditor key={`${revision}-${query.data.set_at}-${query.data.source}`}
    view={query.data} saved={saved} setSaved={setSaved}
    onRestart={() => setRevision((r) => r + 1)} />
}

// Track edits keep untracked: and pinned: naming the same tracks.
function renameTrack(form: Form, track: TrackForm, name: string): Partial<Form> {
  const rename = (n: string) => (n === track.name ? name : n)
  return {
    tracks: form.tracks.map((t) => (t.id === track.id ? { ...t, name } : t)),
    untracked: rename(form.untracked),
    pinned: form.pinned.map(rename),
  }
}

function removeTrack(form: Form, track: TrackForm): Partial<Form> {
  return {
    tracks: form.tracks.filter((t) => t.id !== track.id),
    pinned: form.pinned.filter((p) => p !== track.name),
  }
}

/** A new track starts as a copy of the untracked track's stage lists. */
function addTrack(form: Form): Partial<Form> {
  const base = form.tracks.find((t) => t.name === form.untracked) ?? form.tracks[0]!
  const { spec, plan, implement, review } = base
  const name = `track-${form.tracks.length + 1}`
  return { tracks: [...form.tracks, { id: nextId++, name, when: '', plan_review: true, spec, plan, implement, review }] }
}

type Patch = (change: Partial<Form>) => void

function SettingsEditor({ view, saved, setSaved, onRestart }: {
  view: ModelSettingsView; saved: boolean; setSaved: (saved: boolean) => void; onRestart: () => void
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(() => toForm(view.models))
  const dirty = JSON.stringify(toBody(form)) !== JSON.stringify(toBody(toForm(view.models)))

  const onStored = (next: ModelSettingsView) => {
    queryClient.setQueryData(queryKeys.modelSettings, next)
    // The block decides every next-launch model and the priority options.
    void queryClient.invalidateQueries({ queryKey: queryKeys.usage })
    void queryClient.invalidateQueries({ queryKey: queryKeys.board })
    void queryClient.invalidateQueries({ queryKey: queryKeys.allTasks })
  }
  const save = useMutation({
    mutationFn: () => api.saveModelSettings(toBody(form)),
    onSuccess: (next) => { setSaved(true); onStored(next) },
  })
  const reset = useMutation({
    mutationFn: api.resetModelSettings,
    onSuccess: (next) => { setSaved(false); onStored(next) },
  })
  const patch: Patch = (change) => { setSaved(false); setForm((f) => ({ ...f, ...change })) }

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 p-4">
      <SourceHeader view={view} />
      {Object.keys(view.efforts).map((p) => (
        <datalist key={p} id={`models-${p}`}>
          {view.known_models.map(parseEntry).filter((e) => e.provider === p)
            .map((e) => <option key={e.model} value={e.model} />)}
        </datalist>
      ))}
      <GeneralSection form={form} efforts={view.efforts} patch={patch} />
      <div className="flex items-center justify-between">
        <h2 className="font-semibold">Tracks</h2>
        <button type="button" className={BUTTON} onClick={() => patch(addTrack(form))}>Add track</button>
      </div>
      {form.tracks.map((track) => (
        <TrackCard key={track.id} track={track} form={form} efforts={view.efforts} patch={patch} />
      ))}
      <ActionBar canReset={view.source === 'console'} dirty={dirty} saved={saved}
        saving={save.isPending} busy={save.isPending || reset.isPending} error={save.error ?? reset.error}
        onSave={() => save.mutate()} onDiscard={onRestart} onReset={() => reset.mutate()} />
    </div>
  )
}

function SourceHeader({ view }: { view: ModelSettingsView }) {
  const fromConsole = view.source === 'console'
  return <>
    <header className="flex flex-wrap items-center gap-x-3 gap-y-1">
      <h1 className="text-lg font-semibold">Model settings</h1>
      <span data-testid="settings-source" className={chip[fromConsole ? 'running' : 'neutral']}>
        {fromConsole ? 'set in the console' : 'from targets.yaml'}
      </span>
      {fromConsole && view.set_by && (
        <span className="text-sm text-ink-muted">by {view.set_by} · {relativeTime(view.set_at)}</span>
      )}
    </header>
    <p className="text-sm text-ink-muted">
      Which model, at which effort, runs each stage of each track. A save goes into force on the
      next dispatcher pass and replaces the <code>models:</code> block of targets.yaml until you
      reset it. A stage that already started keeps its model.
    </p>
    {view.error && <p role="alert" className={banner.failed}>{view.error}. The targets.yaml block is in force.</p>}
    {view.target_overrides.length > 0 && (
      <p className={banner.waiting}>
        These targets have their own <code>models:</code> block in targets.yaml, which these settings
        do not change: {view.target_overrides.join(', ')}.
      </p>
    )}
  </>
}

function GeneralSection({ form, efforts, patch }: { form: Form; efforts: Record<string, string[]>; patch: Patch }) {
  const unpinned = form.tracks.map((t) => t.name).filter((n) => !form.pinned.includes(n))
  return (
    <section className={SECTION} aria-labelledby="general-heading">
      <h2 id="general-heading" className="mb-3 font-semibold">General</h2>
      <div className="flex flex-col gap-4">
        <Labeled label="Triage" help="The daily triage sweep runs the first entry the usage gate admits.">
          <EntryList label="Triage" value={form.triage} efforts={efforts}
            onChange={(triage) => patch({ triage })} />
        </Labeled>
        <Labeled label="Untracked" help="The track for a task with no track: label.">
          <select aria-label="Untracked track" className={`${FIELD} w-fit`} value={form.untracked}
            onChange={(e) => patch({ untracked: e.target.value })}>
            {form.tracks.map((t) => <option key={t.id} value={t.name}>{t.name}</option>)}
          </select>
        </Labeled>
        <Labeled label="Pinned tracks"
          help="A pinned track tries its entries in the written order: the priority mode does not reorder them. Sessions pick the first pinned track that fits.">
          <ol className="flex flex-col gap-1.5">
            {form.pinned.map((name, i) => (
              <li key={name} className="flex items-center gap-1.5">
                <span className="min-w-24 text-sm">{i + 1}. {name}</span>
                <MoveButtons index={i} length={form.pinned.length} label={`pinned track ${name}`}
                  onMove={(to) => patch({ pinned: move(form.pinned, i, to) })}
                  onRemove={() => patch({ pinned: form.pinned.filter((p) => p !== name) })} />
              </li>
            ))}
          </ol>
          {unpinned.length > 0 && (
            <select aria-label="Pin a track" className={`${FIELD} mt-1.5 w-fit`} value=""
              onChange={(e) => patch({ pinned: [...form.pinned, e.target.value] })}>
              <option value="" disabled>Pin a track…</option>
              {unpinned.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          )}
        </Labeled>
        <Labeled label="Second review model"
          help="Optional. A non-anthropic provider/model that a Claude review session can also run. Empty: none.">
          <input aria-label="Second review model" className={`${FIELD} w-full max-w-xs font-mono`}
            value={form.review_second} placeholder="openai/…"
            onChange={(e) => patch({ review_second: e.target.value })} />
        </Labeled>
      </div>
    </section>
  )
}

function TrackCard({ track, form, efforts, patch }: {
  track: TrackForm; form: Form; efforts: Record<string, string[]>; patch: Patch
}) {
  const isUntracked = track.name === form.untracked
  const set = (change: Partial<TrackForm>) =>
    patch({ tracks: form.tracks.map((t) => (t.id === track.id ? { ...t, ...change } : t)) })
  return (
    <section className={SECTION} aria-label={`Track ${track.name}`}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input aria-label="Track name" className={`${FIELD} font-mono font-semibold`} value={track.name}
          onChange={(e) => patch(renameTrack(form, track, e.target.value.replace(/[\s:]/g, '')))} />
        {isUntracked && <span className={chip.neutral}>untracked</span>}
        {form.pinned.includes(track.name) && <span className={chip.parked}>pinned</span>}
        <button type="button" className={`${BUTTON} ml-auto`} disabled={isUntracked}
          title={isUntracked ? 'The untracked track cannot be removed' : undefined}
          onClick={() => patch(removeTrack(form, track))}>Remove track</button>
      </div>
      <label className="flex flex-col gap-1 text-sm">
        When
        <textarea aria-label={`When ${track.name}`} rows={2} className={`${FIELD} w-full`}
          value={track.when} onChange={(e) => set({ when: e.target.value })} />
        <span className="text-xs text-ink-muted">Triage and the spec session read this sentence to pick the track.</span>
      </label>
      <label className="mt-3 flex flex-wrap items-center gap-x-2 text-sm">
        <input type="checkbox" checked={track.plan_review}
          onChange={(e) => set({ plan_review: e.target.checked })} />
        <span className="whitespace-nowrap">Plan review gate</span>
        <span className="text-xs text-ink-muted">(off: the gate can be skipped when no question is open)</span>
      </label>
      <div className="mt-3 flex flex-col gap-3">
        {STAGES.map((stage) => (
          <div key={stage} className="flex flex-col gap-1 sm:flex-row sm:gap-3">
            <span className="w-20 shrink-0 pt-1 text-sm capitalize text-ink-muted">{stage}</span>
            <EntryList label={`${track.name} ${stage}`} value={track[stage]} efforts={efforts}
              onChange={(entries) => set({ [stage]: entries })} />
          </div>
        ))}
      </div>
    </section>
  )
}

function ActionBar({ canReset, dirty, saved, saving, busy, error, onSave, onDiscard, onReset }: {
  canReset: boolean; dirty: boolean; saved: boolean; saving: boolean; busy: boolean; error: Error | null
  onSave: () => void; onDiscard: () => void; onReset: () => void
}) {
  return (
    <div className="sticky bottom-0 -mx-4 flex flex-wrap items-center gap-2 border-t border-border bg-surface px-4 py-3">
      <button type="button" className={PRIMARY} disabled={!dirty || busy} onClick={onSave}>
        {saving ? 'Saving…' : 'Save'}
      </button>
      <button type="button" className={BUTTON} disabled={!dirty || busy} onClick={onDiscard}>Discard changes</button>
      {canReset && <ResetButton busy={busy} onReset={onReset} />}
      <span role="status" className="text-sm"><SaveNote error={error} saved={saved} dirty={dirty} /></span>
    </div>
  )
}

/** Deleting the stored block is a two-step action, like kill. */
function ResetButton({ busy, onReset }: { busy: boolean; onReset: () => void }) {
  const [armed, setArmed] = useState(false)
  if (!armed) {
    return <button type="button" className={BUTTON} disabled={busy} onClick={() => setArmed(true)}>
      Reset to targets.yaml
    </button>
  }
  return <>
    <button type="button" className="rounded border border-failed-fg/30 px-3 py-1.5 text-sm text-failed-fg disabled:opacity-50"
      disabled={busy} onClick={onReset}>Confirm reset</button>
    <button type="button" className={BUTTON} onClick={() => setArmed(false)}>Cancel</button>
  </>
}

function SaveNote({ error, saved, dirty }: { error: Error | null; saved: boolean; dirty: boolean }) {
  if (error) return <span className="text-failed-fg">{error instanceof ApiError ? error.detail : String(error)}</span>
  if (dirty) return <span className="text-ink-muted">Unsaved changes</span>
  return saved ? <span className="text-running-fg">Saved. The next dispatcher pass uses it.</span> : null
}

function Labeled({ label, help, children }: { label: string; help: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-sm font-medium">{label}</span>
      <span className="text-xs text-ink-muted">{help}</span>
      <div className="mt-1">{children}</div>
    </div>
  )
}

function MoveButtons({ index, length, label, onMove, onRemove }: {
  index: number; length: number; label: string; onMove: (to: number) => void; onRemove: () => void
}) {
  return <>
    <button type="button" className={ICON} aria-label={`Move ${label} up`} disabled={index === 0}
      onClick={() => onMove(index - 1)}>↑</button>
    <button type="button" className={ICON} aria-label={`Move ${label} down`} disabled={index === length - 1}
      onClick={() => onMove(index + 1)}>↓</button>
    <button type="button" className={ICON} aria-label={`Remove ${label}`} onClick={onRemove}>✕</button>
  </>
}

/** An ordered stage list: the dispatcher tries the entries in this order
 *  (or by required pace, see targets.example.yaml) and launches the first the
 *  usage gate admits. */
function EntryList({ label, value, efforts, onChange }: {
  label: string; value: string[]; efforts: Record<string, string[]>; onChange: (v: string[]) => void
}) {
  const entries = value.map(parseEntry)
  const set = (i: number, change: Partial<Entry>) =>
    onChange(entries.map((e, j) => formatEntry(j === i ? { ...e, ...change } : e)))
  return (
    <ol className="flex min-w-0 flex-1 flex-col gap-1.5">
      {entries.map((entry, i) => {
        const name = `${label} entry ${i + 1}`
        return (
          <li key={i} className="flex flex-wrap items-center gap-1.5 border-l-2 border-border pl-2">
            <select aria-label={`${name} provider`} className={FIELD} value={entry.provider}
              onChange={(e) => set(i, {
                provider: e.target.value,
                effort: (efforts[e.target.value] ?? []).includes(entry.effort) ? entry.effort : '',
              })}>
              {Object.keys(efforts).map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            <input aria-label={`${name} model`} className={`${FIELD} w-44 min-w-0 font-mono`}
              list={`models-${entry.provider}`} value={entry.model} placeholder="model"
              onChange={(e) => set(i, { model: e.target.value.trim() })} />
            <select aria-label={`${name} effort`} className={FIELD} value={entry.effort}
              onChange={(e) => set(i, { effort: e.target.value })}>
              <option value="">default effort</option>
              {(efforts[entry.provider] ?? []).map((x) => <option key={x} value={x}>{x}</option>)}
            </select>
            <MoveButtons index={i} length={entries.length} label={name}
              onMove={(to) => onChange(move(value, i, to))}
              onRemove={() => onChange(value.filter((_, j) => j !== i))} />
          </li>
        )
      })}
      <li>
        <button type="button" className="text-sm text-ink-muted underline hover:text-ink"
          onClick={() => onChange([...value, formatEntry({ provider: 'anthropic', model: '', effort: '' })])}>
          + Add model
        </button>
      </li>
    </ol>
  )
}
