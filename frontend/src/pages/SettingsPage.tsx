import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState, type ReactNode } from 'react'
import { queryKeys } from '../hooks/queryKeys'
import { api, ApiError, type EntrySetting, type ModelSettingsView, type ModelsSetting, type TrackSetting } from '../lib/api'
import { relativeTime } from '../lib/format'
import { banner, chip } from '../lib/tone'

type Stage = Exclude<keyof TrackSetting, 'when' | 'plan_review'>
// Every stage the server sends, in pipeline order: a new one fails the build here.
const STAGE_ORDER: Record<Stage, number> = { spec: 0, plan: 1, implement: 2, review: 3 }
const STAGES = (Object.keys(STAGE_ORDER) as Stage[]).sort((a, b) => STAGE_ORDER[a] - STAGE_ORDER[b])

interface TrackForm extends Record<Stage, EntrySetting[]> {
  id: number            // React key: survives a rename
  name: string
  when: string
  plan_review: boolean
}
// Tracks are a list and untracked/pinned name them by id: a rename must not
// reorder, merge or repoint them, even while two tracks share a name.
interface Form extends Omit<ModelsSetting, 'tracks' | 'untracked' | 'pinned'> {
  tracks: TrackForm[]
  untracked: number
  pinned: number[]
}
type Patch = (change: Partial<Form>) => void
type Efforts = Record<string, string[]>

let nextId = 0

function toForm(models: ModelsSetting): Form {
  const tracks = Object.entries(models.tracks).map(([name, t]) => ({ id: nextId++, name, ...t }))
  const id = (name: string) => tracks.find((t) => t.name === name)?.id ?? -1
  return { ...models, tracks, untracked: id(models.untracked), pinned: models.pinned.map(id) }
}

function toBody(form: Form): ModelsSetting {
  const name = (id: number) => form.tracks.find((t) => t.id === id)?.name ?? ''
  return {
    ...form,
    untracked: name(form.untracked),
    tracks: Object.fromEntries(form.tracks.map(({ id: _id, name, ...t }) => [name, t])),
    pinned: form.pinned.map(name),
  }
}

/** Why a track name cannot be saved, or '': the body maps name to track, so
 *  a second track of the same name would replace the first. */
function nameProblem(form: Form, track: TrackForm): string {
  if (track.name === '') return 'Give the track a name.'
  return form.tracks.some((t) => t.id !== track.id && t.name === track.name)
    ? 'Another track has this name.' : ''
}

function removeTrack(form: Form, track: TrackForm): Partial<Form> {
  return {
    tracks: form.tracks.filter((t) => t.id !== track.id),
    pinned: form.pinned.filter((id) => id !== track.id),
  }
}

/** A new track starts as a copy of the untracked track's stage lists, under
 *  the first `track-<n>` name no track has. */
function addTrack(form: Form): Partial<Form> {
  const base = form.tracks.find((t) => t.id === form.untracked) ?? form.tracks[0]!
  const { spec, plan, implement, review } = base
  const taken = new Set(form.tracks.map((t) => t.name))
  let n = form.tracks.length + 1
  while (taken.has(`track-${n}`)) n++
  return { tracks: [...form.tracks, { id: nextId++, name: `track-${n}`, when: '', plan_review: true, spec, plan, implement, review }] }
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
  if (query.isPending) return <p className="p-4 text-ink-muted">loading model settings…</p>
  if (query.isError) {
    return <p className="p-4 text-failed-fg">model settings unavailable: {query.error.message}</p>
  }
  return <SettingsEditor view={query.data} />
}

function SettingsEditor({ view }: { view: ModelSettingsView }) {
  const queryClient = useQueryClient()
  // The view the form started from. Its revision goes with a save or reset,
  // so a newer view fetched in the background never vouches for edits made
  // on an older one: the server refuses them (409).
  const [base, setBase] = useState(view)
  const [form, setForm] = useState(() => toForm(view.models))
  const [saved, setSaved] = useState(false)
  const baseline = useMemo(() => JSON.stringify(toBody(toForm(base.models))), [base.models])
  const dirty = JSON.stringify(toBody(form)) !== baseline
  const invalid = form.tracks.some((t) => nameProblem(form, t) !== '')
  const restart = (next: ModelSettingsView) => { setBase(next); setForm(toForm(next.models)) }

  // A save or a reset starts the form over from what the server stored.
  const onStored = (next: ModelSettingsView, wasSave: boolean) => {
    queryClient.setQueryData(queryKeys.modelSettings, next)
    restart(next)
    setSaved(wasSave)
    // The block decides every next-launch model and the priority options.
    void queryClient.invalidateQueries({ queryKey: queryKeys.usage })
    void queryClient.invalidateQueries({ queryKey: queryKeys.board })
    void queryClient.invalidateQueries({ queryKey: queryKeys.allTasks })
  }
  const save = useMutation({
    mutationFn: () => api.saveModelSettings(base.revision, toBody(form)),
    onSuccess: (next) => onStored(next, true),
  })
  const restore = useMutation({
    mutationFn: () => api.resetModelSettings(base.revision),
    onSuccess: (next) => onStored(next, false),
  })
  const patch: Patch = (change) => { setSaved(false); setForm((f) => ({ ...f, ...change })) }
  const busy = save.isPending || restore.isPending
  const newer = view.revision !== base.revision
  // With no edits, follow a newer view at once (another tab or operator saved).
  if (newer && !dirty && !busy) restart(view)
  const newEntry = (): EntrySetting => ({ provider: base.default_provider, model: '', effort: '' })

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-4 p-4">
      <SourceHeader view={base} />
      {newer && dirty && (
        <p role="alert" className={banner.waiting}>
          Someone saved newer model settings while you were editing. Discard your changes to load them;
          a save of these changes will be refused.
        </p>
      )}
      {Object.entries(base.known_models).map(([provider, models]) => (
        <datalist key={provider} id={`models-${provider}`}>
          {models.map((m) => <option key={m} value={m} />)}
        </datalist>
      ))}
      {/* Locked while a save or reset waits (up to a dispatcher pass): the
          answer replaces the form, so an edit made meanwhile would be lost. */}
      <fieldset disabled={busy} className="flex min-w-0 flex-col gap-4">
        <GeneralSection form={form} efforts={base.efforts} newEntry={newEntry} patch={patch} />
        <div className="flex items-center justify-between">
          <h2 className="font-semibold">Tracks</h2>
          <button type="button" className={BUTTON} onClick={() => patch(addTrack(form))}>Add track</button>
        </div>
        {form.tracks.map((track) => (
          <TrackCard key={track.id} track={track} form={form} efforts={base.efforts}
            newEntry={newEntry} patch={patch} />
        ))}
      </fieldset>
      <ActionBar canReset={base.source === 'console'} dirty={dirty} invalid={invalid} saved={saved}
        saving={save.isPending} busy={busy} error={save.error ?? restore.error}
        onSave={() => save.mutate()}
        onDiscard={() => { setSaved(false); save.reset(); restore.reset(); restart(view) }}
        onReset={() => restore.mutate()} />
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
    {view.project_overrides.length > 0 && (
      <p className={banner.waiting}>
        These projects have their own <code>models:</code> block in targets.yaml, which these settings
        do not change: {view.project_overrides.join(', ')}.
      </p>
    )}
  </>
}

function GeneralSection({ form, efforts, newEntry, patch }: {
  form: Form; efforts: Efforts; newEntry: () => EntrySetting; patch: Patch
}) {
  const unpinned = form.tracks.filter((t) => !form.pinned.includes(t.id))
  const nameOf = (id: number) => form.tracks.find((t) => t.id === id)?.name ?? ''
  // The second review model runs on another provider than the default one.
  const secondEntry = (): EntrySetting => {
    const entry = newEntry()
    return { ...entry, provider: Object.keys(efforts).find((p) => p !== entry.provider) ?? entry.provider }
  }
  return (
    <section className={SECTION} aria-labelledby="general-heading">
      <h2 id="general-heading" className="mb-3 font-semibold">General</h2>
      <div className="flex flex-col gap-4">
        <Labeled label="Triage" help="The daily triage sweep runs the first entry the usage gate admits.">
          <EntryList label="Triage" value={form.triage} efforts={efforts} newEntry={newEntry}
            onChange={(triage) => patch({ triage })} />
        </Labeled>
        <Labeled label="Untracked" help="The track for a task with no track: label.">
          <select aria-label="Untracked track" className={`${FIELD} w-fit`} value={form.untracked}
            onChange={(e) => patch({ untracked: Number(e.target.value) })}>
            {form.tracks.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
        </Labeled>
        <Labeled label="Pinned tracks"
          help="A pinned track tries its entries in the written order: the priority mode does not reorder them. Sessions pick the first pinned track that fits.">
          <ol className="flex flex-col gap-1.5">
            {form.pinned.map((id, i) => (
              <li key={id} className="flex items-center gap-1.5">
                <span className="min-w-24 text-sm">{i + 1}. {nameOf(id)}</span>
                <MoveButtons index={i} length={form.pinned.length} label={`pinned track ${nameOf(id)}`}
                  onMove={(to) => patch({ pinned: move(form.pinned, i, to) })}
                  onRemove={() => patch({ pinned: form.pinned.filter((p) => p !== id) })} />
              </li>
            ))}
          </ol>
          {unpinned.length > 0 && (
            <select aria-label="Pin a track" className={`${FIELD} mt-1.5 w-fit`} value=""
              onChange={(e) => patch({ pinned: [...form.pinned, Number(e.target.value)] })}>
              <option value="" disabled>Pin a track…</option>
              {unpinned.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </select>
          )}
        </Labeled>
        <Labeled label="Second review model"
          help="Optional. A model of another provider that a Claude review session can also run, at its own configured effort.">
          <EntryList label="Second review" value={form.review_second ? [form.review_second] : []}
            efforts={efforts} newEntry={secondEntry} single
            onChange={([second]) => patch({ review_second: second ?? null })} />
        </Labeled>
      </div>
    </section>
  )
}

function TrackCard({ track, form, efforts, newEntry, patch }: {
  track: TrackForm; form: Form; efforts: Efforts; newEntry: () => EntrySetting; patch: Patch
}) {
  const isUntracked = track.id === form.untracked
  const problem = nameProblem(form, track)
  const set = (change: Partial<TrackForm>) =>
    patch({ tracks: form.tracks.map((t) => (t.id === track.id ? { ...t, ...change } : t)) })
  return (
    <section className={SECTION} aria-label={`Track ${track.name}`}>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input aria-label="Track name" aria-invalid={problem !== ''} aria-describedby={`name-${track.id}`}
          className={`${FIELD} font-mono font-semibold ${problem ? 'border-failed-fg' : ''}`} value={track.name}
          onChange={(e) => set({ name: e.target.value.replace(/[\s:]/g, '') })} />
        {isUntracked && <span className={chip.neutral}>untracked</span>}
        {form.pinned.includes(track.id) && <span className={chip.parked}>pinned</span>}
        <button type="button" className={`${BUTTON} ml-auto`} disabled={isUntracked}
          title={isUntracked ? 'The untracked track cannot be removed' : undefined}
          onClick={() => patch(removeTrack(form, track))}>Remove track</button>
        {problem && <p id={`name-${track.id}`} className="basis-full text-xs text-failed-fg">{problem}</p>}
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
              newEntry={newEntry} onChange={(entries) => set({ [stage]: entries })} />
          </div>
        ))}
      </div>
    </section>
  )
}

function ActionBar({ canReset, dirty, invalid, saved, saving, busy, error, onSave, onDiscard, onReset }: {
  canReset: boolean; dirty: boolean; invalid: boolean; saved: boolean; saving: boolean; busy: boolean
  error: Error | null; onSave: () => void; onDiscard: () => void; onReset: () => void
}) {
  return (
    <div className="sticky bottom-0 -mx-4 flex flex-wrap items-center gap-2 border-t border-border bg-surface px-4 py-3">
      <button type="button" className={PRIMARY} disabled={!dirty || invalid || busy} onClick={onSave}>
        {saving ? 'Saving…' : 'Save'}
      </button>
      <button type="button" className={BUTTON} disabled={!dirty || busy} onClick={onDiscard}>Discard changes</button>
      {canReset && <ResetButton busy={busy} onReset={onReset} />}
      <span role="status" className="text-sm">
        <SaveNote error={error} invalid={invalid} saved={saved} dirty={dirty} />
      </span>
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

function SaveNote({ error, invalid, saved, dirty }: {
  error: Error | null; invalid: boolean; saved: boolean; dirty: boolean
}) {
  if (error) return <span className="text-failed-fg">{error instanceof ApiError ? error.detail : String(error)}</span>
  if (invalid) return <span className="text-failed-fg">Fix the track names to save.</span>
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
 *  usage gate admits. `single`: at most one entry, with no effort of its own. */
function EntryList({ label, value, efforts, newEntry, onChange, single = false }: {
  label: string; value: EntrySetting[]; efforts: Efforts; newEntry: () => EntrySetting
  onChange: (v: EntrySetting[]) => void; single?: boolean
}) {
  return (
    <ol className="flex min-w-0 flex-1 flex-col gap-1.5">
      {value.map((entry, i) => {
        const name = `${label} entry ${i + 1}`
        return (
          <EntryRow key={i} name={name} entry={entry} efforts={efforts} withEffort={!single}
            onChange={(next) => onChange(value.map((e, j) => (j === i ? next : e)))}>
            {single
              ? <button type="button" className={ICON} aria-label={`Remove ${name}`} onClick={() => onChange([])}>✕</button>
              : <MoveButtons index={i} length={value.length} label={name}
                onMove={(to) => onChange(move(value, i, to))}
                onRemove={() => onChange(value.filter((_, j) => j !== i))} />}
          </EntryRow>
        )
      })}
      {!(single && value.length > 0) && (
        <li>
          <button type="button" className="text-sm text-ink-muted underline hover:text-ink"
            onClick={() => onChange([...value, newEntry()])}>
            + Add model
          </button>
        </li>
      )}
    </ol>
  )
}

/** One entry: provider, model (suggested from the models this provider
 *  runs), effort; a provider change drops an effort the new one lacks. */
function EntryRow({ name, entry, efforts, withEffort, onChange, children }: {
  name: string; entry: EntrySetting; efforts: Efforts; withEffort: boolean
  onChange: (e: EntrySetting) => void; children: ReactNode
}) {
  const vocabulary = efforts[entry.provider] ?? []
  return (
    <li className="flex flex-wrap items-center gap-1.5 border-l-2 border-border pl-2">
      <select aria-label={`${name} provider`} className={FIELD} value={entry.provider}
        onChange={(e) => onChange({
          ...entry, provider: e.target.value,
          effort: (efforts[e.target.value] ?? []).includes(entry.effort) ? entry.effort : '',
        })}>
        {Object.keys(efforts).map((p) => <option key={p} value={p}>{p}</option>)}
      </select>
      <input aria-label={`${name} model`} className={`${FIELD} w-44 min-w-0 font-mono`}
        list={`models-${entry.provider}`} value={entry.model} placeholder="model"
        onChange={(e) => onChange({ ...entry, model: e.target.value.trim() })} />
      {withEffort && (
        <select aria-label={`${name} effort`} className={FIELD} value={entry.effort}
          onChange={(e) => onChange({ ...entry, effort: e.target.value })}>
          <option value="">default effort</option>
          {vocabulary.map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
      )}
      {children}
    </li>
  )
}
