import { useId, useState, type FormEvent, type ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api, type OnboardingAccepted, type OnboardingStartRequest, type OnboardingRequest } from '../../lib/api'
import { banner, chip } from '../../lib/tone'

export const panel = 'rounded-lg border border-border bg-surface-raised p-5 md:p-6'
export const button = 'min-h-11 rounded-md border border-border px-4 py-2 text-sm font-medium disabled:opacity-50'
export const primary = `${button} border-ink bg-ink text-surface-raised`
const inputClass = 'mt-1.5 w-full min-w-0 rounded-md border border-border bg-surface-raised px-3 py-2.5 text-ink'

type Commands = OnboardingRequest['commands'][number]
type RepositoryDraft = Commands & { repo: string; id: number }
const emptyCommands = { setup_cmd: '', gate_cmd: '', verify_cmd: '' }

function Field({ label, hint, command, ...props }: {
  label: string; hint: string; command?: boolean
} & React.InputHTMLAttributes<HTMLInputElement>) {
  const id = useId()
  return <div><label htmlFor={id} className="text-sm font-medium">{label}</label>
    <input {...props} id={id} aria-describedby={`${id}-hint`} className={`${inputClass} ${command ? 'font-mono text-sm' : ''}`} />
    <p id={`${id}-hint`} className="mt-1 text-xs text-ink-muted">{hint}</p>
  </div>
}

export function PausedNote() {
  return <div className="rounded-md bg-surface-attention p-4 text-sm">
    <p className="font-semibold">New projects start paused</p>
    <p className="mt-1 leading-relaxed">Review the proposal first. You’ll merge an infrastructure PR before the project appears on the board. It won’t pick up tasks until enabled.</p>
  </div>
}

function RepositoryFields({ draft, number, remove, update, locked }: {
  draft: RepositoryDraft; number: number; remove?: () => void
  update: (change: Partial<RepositoryDraft>) => void; locked: boolean
}) {
  return <fieldset className="space-y-4 border-t border-border pt-5">
    <legend className="sr-only">Repository {number}</legend>
    <div className="flex items-center justify-between gap-3"><h3 className="font-semibold">Repository {number}</h3>
      {remove && <button type="button" onClick={remove} className={button}>Remove repository {number}</button>}</div>
    <Field label="GitHub repository" hint="owner/repository or an HTTPS URL" required value={draft.repo} disabled={locked}
      onChange={e => update({ repo: e.target.value })} />
    <Field label="Setup command" hint="Optional. Installs dependencies before work starts." command value={draft.setup_cmd}
      onChange={e => update({ setup_cmd: e.target.value })} />
    <Field label="Gate command" hint="Required. Checks that each change is ready." required command value={draft.gate_cmd}
      onChange={e => update({ gate_cmd: e.target.value })} />
    <Field label="Verification command" hint="Optional. Runs end-to-end checks." command value={draft.verify_cmd}
      onChange={e => update({ verify_cmd: e.target.value })} />
  </fieldset>
}

function initialDraft(request?: OnboardingRequest): RepositoryDraft[] {
  if (!request) return [{ ...emptyCommands, repo: '', id: 0 }]
  return request.repos.map((repo, id) => ({ ...emptyCommands, ...request.commands[id]!, repo, id }))
}

function Summary({ name, board, count }: { name: string; board: string; count: number }) {
  return <aside className={`${panel} self-start`} aria-label="Project at a glance"><h2 className="font-semibold">Project at a glance</h2>
    <dl className="my-5 space-y-4 text-sm">
      <div><dt className="text-ink-muted">Project</dt><dd className="mt-1 break-words">{name || 'Unnamed'}</dd></div>
      <div><dt className="text-ink-muted">Board</dt><dd className="mt-1 break-words">{board || 'Not set'}</dd></div>
      <div><dt className="text-ink-muted">Repositories</dt><dd className="mt-1">{count}</dd></div>
      <div><dt className="text-ink-muted">When onboarding finishes</dt><dd className="mt-1"><span className={chip.waiting}>Paused</span></dd></div>
    </dl><PausedNote /></aside>
}

function SubmitButton({ pending, editing }: { pending: boolean; editing: boolean }) {
  return <button type="submit" className={primary}>{pending ? 'Submitting…' : editing ? 'Save details' : 'Start onboarding'}</button>
}

export function OnboardingForm({ project = '', request, requestRevision, repositoriesLocked = false, onSaved, disabled = false, feedback }: {
  project?: string; request?: OnboardingRequest; requestRevision?: string; repositoriesLocked?: boolean
  onSaved: (project: string, accepted: OnboardingAccepted) => void | Promise<void>
  disabled?: boolean; feedback?: ReactNode
}) {
  const [name, setName] = useState(project)
  const [board, setBoard] = useState(request ? `${request.board.owner}/${request.board.number}` : '')
  const [repos, setRepos] = useState(() => initialDraft(request))
  const mutation = useMutation({ mutationFn: (body: OnboardingStartRequest) => api.startOnboarding(name, body), onSuccess: accepted => onSaved(name, accepted) })
  const update = (id: number, change: Partial<RepositoryDraft>) => setRepos(rows => rows.map(r => r.id === id ? { ...r, ...change } : r))
  const remove = (id: number) => setRepos(rows => rows.filter(r => r.id !== id))
  const add = () => setRepos(rows => [...rows, { ...emptyCommands, repo: '', id: Math.max(...rows.map(r => r.id)) + 1 }])
  const submit = (event: FormEvent) => {
    event.preventDefault()
    const [owner, number] = board.split('/')
    mutation.mutate({ expected_request_revision: requestRevision, repos: repos.map(r => r.repo.trim()), board: { owner: owner!, number: Number(number) },
      commands: repos.map(({ setup_cmd, gate_cmd, verify_cmd }) => ({ setup_cmd, gate_cmd, verify_cmd })) })
  }
  return <form onSubmit={submit} className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
    <fieldset disabled={mutation.isPending || disabled} className="min-w-0 space-y-5">
      <section className={panel}><h2 className="text-xl font-semibold">Project</h2><div className="mt-5 grid gap-5 md:grid-cols-2">
        <Field label="Project name" hint="Lowercase letters, numbers and hyphens. Cannot change later." required pattern="[a-z0-9-]+" value={name} disabled={Boolean(project)} onChange={e => setName(e.target.value)} />
        <Field label="GitHub board" hint="Existing board, in owner/number format." required pattern="[^/\s]+/[1-9][0-9]*" value={board} onChange={e => setBoard(e.target.value)} />
      </div></section>
      <section className={`${panel} space-y-5`}><div><h2 className="text-xl font-semibold">Repositories &amp; commands</h2><p className="mt-1 text-sm text-ink-muted">Each repository has its own commands.</p></div>
        {repos.map((draft, index) => <RepositoryFields key={draft.id} draft={draft} number={index + 1} locked={repositoriesLocked}
          update={change => update(draft.id, change)} remove={repos.length > 1 && !repositoriesLocked ? () => remove(draft.id) : undefined} />)}
        {!repositoriesLocked && <button type="button" onClick={add} className={button}>Add repository</button>}
        {feedback}
        {mutation.error && <p role="alert" className={banner.failed}>{mutation.error.message}</p>}
        <div className="flex flex-wrap justify-end gap-3"><SubmitButton pending={mutation.isPending} editing={Boolean(request)} /></div>
      </section>
    </fieldset><Summary name={name} board={board} count={repos.length} />
  </form>
}
