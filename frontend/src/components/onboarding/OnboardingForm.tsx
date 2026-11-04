import { useId, useState, type FormEvent, type ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api, type OnboardingAccepted, type OnboardingStartRequest, type OnboardingRequest, type OnboardingView, type OnboardingProposal } from '../../lib/api'
import { RepositoryFacts, Source, type RepositoryInspection, type RepositoryProposal } from './InspectionFields'
import { banner, chip } from '../../lib/tone'

export const panel = 'rounded-lg border border-border bg-surface-raised p-5 md:p-6'
export const button = 'min-h-11 rounded-md border border-border px-4 py-2 text-sm font-medium disabled:opacity-50'
export const primary = `${button} border-ink bg-ink text-surface-raised`
const inputClass = 'mt-1.5 w-full min-w-0 rounded-md border border-border bg-surface-raised px-3 py-2.5 text-ink disabled:cursor-not-allowed disabled:bg-surface disabled:text-ink-muted'

type Commands = NonNullable<OnboardingRequest['commands']>[number]
type Detection = { original: string; proposal: RepositoryProposal; inspection?: RepositoryInspection }
type RepositoryDraft = Commands & { repo: string; id: number; detection?: Detection }
const emptyCommands = { setup_cmd: null, gate_cmd: null, verify_cmd: null, services: null }

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

function RepositoryFields({ draft, number, remove, update, locked, inspection, proposal }: {
  draft: RepositoryDraft; number: number; remove?: () => void
  update: (change: Partial<RepositoryDraft>) => void; locked: boolean
  inspection?: RepositoryInspection; proposal?: RepositoryProposal
}) {
  return <fieldset className="space-y-4 border-t border-border pt-5">
    <legend className="sr-only">Repository {number}</legend>
    <div className="flex items-center justify-between gap-3"><h3 className="font-semibold">Repository {number}</h3>
      {remove && <button type="button" onClick={remove} className={button}>Remove repository {number}</button>}</div>
    <Field label="GitHub repository" hint="owner/repository or an HTTPS URL" required value={draft.repo} disabled={locked}
      onChange={e => update({ repo: e.target.value })} />
    <CommandField label="Setup command" hint="Optional. Installs dependencies before work starts." name="setup_cmd" draft={draft} proposal={proposal} inspection={inspection} update={update} />
    <CommandField label="Gate command" hint="Required before confirmation. Leave blank to detect it." name="gate_cmd" draft={draft} proposal={proposal} inspection={inspection} update={update} />
    <CommandField label="Verification command" hint="Optional. Runs end-to-end checks." name="verify_cmd" draft={draft} proposal={proposal} inspection={inspection} update={update} />
    {inspection && <RepositoryFacts inspection={inspection} selected={draft.services ?? proposal?.services ?? []} selectionSource={draft.services == null ? undefined : 'you'} onServices={services => update({ services })} />}
  </fieldset>
}

function CommandField({ label, hint, name, draft, proposal, inspection, update }: {
  label: string; hint: string; name: 'setup_cmd' | 'gate_cmd' | 'verify_cmd'; draft: RepositoryDraft
  proposal?: RepositoryProposal; inspection?: RepositoryInspection; update: (change: Partial<RepositoryDraft>) => void
}) {
  const value = draft[name] ?? proposal?.[name] ?? ''
  const source = draft[name] == null ? inspection?.fields[name]?.source : 'you'
  return <div><Field label={label} hint={hint} command value={value} onChange={event => update({ [name]: event.target.value })} />
    <Source source={source} inspected={inspection?.state === 'complete'} skipped={inspection?.state === 'skipped'} /></div>
}

type AnyBoard = OnboardingRequest['board'] | OnboardingProposal['board']

function boardText(board: AnyBoard) {
  return board && 'number' in board ? `${board.owner}/${board.number}` : ''
}

/** owner/number, or the title a new board gets (the proposal's, or the one the box will give it); '' when there is none. */
export function boardLabel(board: AnyBoard, project: string) {
  if (!board || !('new' in board)) return boardText(board)
  return `New board: ${'title' in board ? board.title : `${project || '<project>'} Backlog`}`
}

function BoardField({ project, value, newBoard, onBoard, onNewBoard, source, inspected }: {
  project: string; value: string; newBoard: boolean; onBoard: (value: string) => void
  onNewBoard: (value: boolean) => void; source?: string; inspected: boolean
}) {
  return <div>
    <Field label="GitHub board" hint={newBoard ? 'Onboarding creates the board after you confirm.' : 'owner/number. Leave blank to detect committed board details.'}
      pattern="[^/\s]+/[1-9][0-9]*" value={value} disabled={newBoard} onChange={e => onBoard(e.target.value)} />
    <label className="mt-2 flex min-h-11 items-center gap-2 text-sm"><input type="checkbox" className="size-4" checked={newBoard} onChange={e => onNewBoard(e.target.checked)} />Create a new board</label>
    {newBoard ? <p className="text-xs text-ink-muted">{boardLabel({ new: true }, project)}, owned by the first repository’s owner.</p>
      : <Source source={source} inspected={inspected} />}
  </div>
}

function newBoardChosen(request?: OnboardingRequest, proposal?: OnboardingProposal | null) {
  if (request?.board) return 'new' in request.board
  return Boolean(proposal?.board && 'new' in proposal.board)
}

function repositoryKey(spelling: string) {
  return spelling.trim().replace(/^https?:\/\/github\.com\//i, '').replace(/\/$/, '').replace(/\.git$/, '').toLowerCase()
}

function detectionFor(draft: RepositoryDraft): Detection | undefined {
  if (!draft.detection) return undefined
  const { original, proposal } = draft.detection
  const aliases = [repositoryKey(original), repositoryKey(proposal.repo)]
  return aliases.includes(repositoryKey(draft.repo)) ? draft.detection : undefined
}

// After resolution, proposal rows follow request order even when GitHub renamed a repository.
// Keep that association on each draft row so removing or adding rows cannot reassign it.
function initialDraft(request?: OnboardingRequest, proposal?: OnboardingProposal | null, inspection?: OnboardingView['inspection']): RepositoryDraft[] {
  if (!request) return [{ ...emptyCommands, repo: '', id: 0 }]
  return request.repos.map((repo, id) => {
    const resolved = proposal?.repositories[id]
    const detection = resolved ? { original: repo, proposal: resolved,
      inspection: inspection?.repositories?.find(item => item.repo === resolved.repo) } : undefined
    return { ...emptyCommands, ...request.commands?.[id], repo, id, detection }
  })
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

export function OnboardingForm({ project = '', request, requestRevision, repositoriesLocked = false, onSaved, disabled = false, feedback, proposal, inspection }: {
  project?: string; request?: OnboardingRequest; requestRevision?: string; repositoriesLocked?: boolean
  onSaved: (project: string, accepted: OnboardingAccepted) => void | Promise<void>
  disabled?: boolean; feedback?: ReactNode; proposal?: OnboardingProposal | null; inspection?: OnboardingView['inspection']
}) {
  const [name, setName] = useState(project)
  const [board, setBoard] = useState<string | null>(() => request?.board ? boardText(request.board) : null)
  const [newBoard, setNewBoard] = useState(() => newBoardChosen(request, proposal))
  const shownBoard = newBoard ? '' : board ?? boardText(proposal?.board)
  const [repos, setRepos] = useState(() => initialDraft(request, proposal, inspection))
  const mutation = useMutation({ mutationFn: (body: OnboardingStartRequest) => api.startOnboarding(name, body), onSuccess: accepted => onSaved(name, accepted) })
  const update = (id: number, change: Partial<RepositoryDraft>) => setRepos(rows => rows.map(r => r.id === id ? { ...r, ...change } : r))
  const remove = (id: number) => setRepos(rows => rows.filter(r => r.id !== id))
  const add = () => setRepos(rows => [...rows, { ...emptyCommands, repo: '', id: Math.max(...rows.map(r => r.id)) + 1 }])
  const submit = (event: FormEvent) => {
    event.preventDefault()
    const [owner, number] = (board ?? '').split('/')
    mutation.mutate({ inspect: request?.inspect ?? true, expected_request_revision: requestRevision, repos: repos.map(r => r.repo.trim()), board: newBoard ? { new: true } : board ? { owner: owner!, number: Number(number) } : null,
      commands: repos.map(({ setup_cmd, gate_cmd, verify_cmd, services }) => ({ setup_cmd, gate_cmd, verify_cmd, services })) })
  }
  return <form onSubmit={submit} className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
    <fieldset disabled={mutation.isPending || disabled} className="min-w-0 space-y-5">
      <section className={panel}><h2 className="text-xl font-semibold">Project</h2><div className="mt-5 grid gap-5 md:grid-cols-2">
        <Field label="Project name" hint="Lowercase letters, numbers and hyphens. Cannot change later." required pattern="[a-z0-9-]+" value={name} disabled={Boolean(project)} onChange={e => setName(e.target.value)} />
        <BoardField project={name} value={shownBoard} newBoard={newBoard} onBoard={setBoard} onNewBoard={setNewBoard}
          source={board === null ? inspection?.repositories?.find(item => item.board.source)?.board.source : 'you'}
          inspected={Boolean(inspection?.repositories?.length)} />
      </div></section>
      <section className={`${panel} space-y-5`}><div><h2 className="text-xl font-semibold">Repositories &amp; commands</h2><p className="mt-1 text-sm text-ink-muted">The box inspects each repository and suggests commands. You can enter known values now.</p></div>
        {repos.map((draft, index) => <RepositoryFields key={draft.id} draft={draft} number={index + 1} locked={repositoriesLocked}
          proposal={detectionFor(draft)?.proposal} inspection={detectionFor(draft)?.inspection}
          update={change => update(draft.id, change)} remove={repos.length > 1 && !repositoriesLocked ? () => remove(draft.id) : undefined} />)}
        {!repositoriesLocked && <button type="button" onClick={add} className={button}>Add repository</button>}
        {feedback}
        {mutation.error && <p role="alert" className={banner.failed}>{mutation.error.message}</p>}
        <div className="flex flex-wrap justify-end gap-3"><SubmitButton pending={mutation.isPending} editing={Boolean(request)} /></div>
      </section>
    </fieldset><Summary name={name} board={newBoard ? boardLabel({ new: true }, name) : shownBoard} count={repos.length} />
  </form>
}
