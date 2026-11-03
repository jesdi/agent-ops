import type { OnboardingView, OnboardingProposal } from '../../lib/api'

type Inspection = NonNullable<NonNullable<OnboardingView['inspection']>['repositories']>[number]
export type RepositoryInspection = Inspection
export type RepositoryProposal = OnboardingProposal['repositories'][number]

export function Source({ source, inspected, skipped = false }: { source?: string; inspected: boolean; skipped?: boolean }) {
  return <p className="mt-1 break-words text-xs text-ink-muted">{source || (skipped ? 'Draft skipped' : inspected ? 'Nothing found' : 'Not inspected yet')}</p>
}

export function RepositoryFacts({ inspection, selected, onServices, selectionSource }: {
  inspection: Inspection; selected: string[]; onServices?: (values: string[]) => void; selectionSource?: string
}) {
  return <div className="space-y-4 border-t border-border pt-4">
    <fieldset><legend className="text-sm font-medium">Compose services</legend>
      {inspection.services.length ? <div className="mt-2 space-y-2">{inspection.services.map(service => <div key={service.value}>
        {onServices ? <label className="flex min-h-9 items-center gap-2 text-sm"><input type="checkbox" checked={selected.includes(service.value)}
          onChange={event => onServices(event.target.checked ? [...selected, service.value] : selected.filter(value => value !== service.value))} />{service.value}</label>
          : <p className="text-sm">{service.value}{selected.includes(service.value) ? ' — selected' : ' — not selected'}</p>}
        <Source source={selected.includes(service.value) ? selectionSource ?? inspection.selected_services?.find(item => item.value === service.value)?.source ?? service.source : service.source} inspected />
      </div>)}</div> : <Source inspected />}
      <p className="mt-2 text-xs text-ink-muted">Recorded for the project. Services are not started by the box yet.</p>
    </fieldset>
    <div><h4 className="text-sm font-medium">Secret names</h4>{inspection.secrets.length
      ? <ul className="mt-2 space-y-2">{inspection.secrets.map(secret => <li key={secret.value} className="text-sm"><span className="break-all">{secret.value}</span><Source source={secret.source} inspected /></li>)}</ul>
      : <Source inspected />}
      <p className="mt-2 text-xs text-ink-muted">Only names are recorded.</p>
    </div>
  </div>
}
