import type { KeyboardEvent } from 'react'
import { useSetPriority } from '../hooks/useSetPriority'
import type { GateView, PriorityView, ProviderUsageView, Severity, UsageView, WindowKind, WindowView } from '../lib/api'
import { formatDuration } from '../lib/format'
import { banner, chip } from '../lib/tone'

const FILL: Record<Severity, string> = {
  ok: 'bg-running-fg', close: 'bg-waiting-fg', blocked: 'bg-failed-fg',
}
const STATE: Record<Severity, string> = {
  ok: 'on pace', close: 'close to the limit', blocked: 'over the limit',
}

/** A headroom fraction as percentage points: a true minus sign, "1 pt" singular. */
function points(x: number): string {
  const v = Math.round(x * 1000) / 10
  const magnitude = Math.abs(v)
  const digits = Number.isInteger(magnitude) ? String(magnitude) : magnitude.toFixed(1)
  return `${v < 0 ? '−' : ''}${digits} ${magnitude === 1 ? 'pt' : 'pts'}`
}

type Kind = {
  label: (w: WindowView) => string
  foot: (w: WindowView) => string
  pace: (w: WindowView) => string | null
}

/** Per window kind: the row label, the label under the head line — the
 *  session window shows its cap, a weekly window its headroom — and the
 *  required pace, which only a weekly window has. */
const KIND: Record<WindowKind, Kind> = {
  session: {
    label: () => 'Session · 5h',
    foot: (w) => `cap ${Math.round(w.allowance * 100)}%`,
    pace: () => null,
  },
  weekly: {
    label: (w) => (w.scope ? `Week · ${w.scope}` : 'Week · all'),
    foot: (w) => `headroom ${points(w.headroom)}`,
    pace: (w) => (w.required_pace == null ? null : `${w.required_pace.toFixed(1)}× pace`),
  },
}

/** Headroom bullet: the hatched band is what the box may have spent by now
 *  (allowance), the fill is what was spent, the gap is headroom — the gate's
 *  actual input. Remaining is spelled out to the right, over the pace the
 *  window's remaining quota asks for. */
function Bullet({ provider, w }: { provider: string; w: WindowView }) {
  const used = Math.round(w.used * 100)
  const allowed = Math.round(w.allowance * 100)
  const remaining = 100 - used
  const label = KIND[w.kind].label(w)
  const pace = KIND[w.kind].pace(w)

  // The sub-track label's own anchor slides with the head line: left-aligned
  // at 0%, right-aligned at 100%, and in between that same fraction of the
  // label sits on the line — so it stays inside any track at least as wide
  // as the label, however narrow the provider column gets.
  const headPct = Math.min(100, allowed)
  const footStyle: React.CSSProperties = { left: `${headPct}%`, transform: `translateX(-${headPct}%)` }

  return (
    // minmax(7rem,1fr) keeps the track column from collapsing to 0: the grid
    // has no intrinsic width there, since every progressbar child is absolute.
    <div className="grid grid-cols-[92px_minmax(7rem,1fr)_auto] items-center gap-2.5 text-xs">
      <span className="text-ink-muted">
        {label}
        <small className="block text-[11px] text-ink-muted">{formatDuration(w.minutes_to_reset * 60)}</small>
      </span>
      <div className="mb-3.5 min-w-0">
        <div
          role="progressbar"
          aria-label={`${provider} ${label} used`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={used}
          aria-valuetext={`${used}% used of ${allowed}% allowed now, ${remaining}% remaining, ${STATE[w.severity]}`}
          className="relative h-3.5 rounded-sm bg-ink/10"
        >
          <div
            className="absolute inset-y-0 left-0 rounded-sm bg-ink/15 [background-image:repeating-linear-gradient(135deg,transparent_0_3px,color-mix(in_oklab,var(--color-ink)_12%,transparent)_3px_4px)]"
            style={{ width: `${headPct}%` }}
          />
          <div className={`absolute inset-y-[3px] left-0 rounded-sm ${FILL[w.severity]}`} style={{ width: `${used}%` }} />
          <div className="absolute inset-y-0 border-l-2 border-ink" style={{ left: `${headPct}%` }} />
          <span
            className="absolute top-4 whitespace-nowrap text-[11px] text-ink-muted"
            style={footStyle}
          >
            {KIND[w.kind].foot(w)}
          </span>
        </div>
      </div>
      <span className="whitespace-nowrap text-right font-semibold text-ink">
        {remaining}% left
        {/* On the headroom's line, in its own column: under the track the two
            together outgrow a narrow track and run into their neighbours. */}
        {pace && <small className="block text-[11px] font-normal text-ink-muted">{pace}</small>}
      </span>
    </div>
  )
}

/** The verdict for the policy default model — what an idle box spawns next.
 *  Shown once, on that model's provider; the provider heading names it, so
 *  the chip names only the model. The note says why, on hover. */
function SpawnChip({ gate }: { gate: GateView }) {
  const model = gate.model.slice(gate.model.indexOf('/') + 1)
  return (
    <span
      title={gate.note}
      className={`min-w-0 truncate normal-case tracking-normal ${chip[gate.admitted ? 'running' : 'failed']}`}
    >
      {`${gate.admitted ? 'will spawn' : 'will not spawn'} ${model}`}
    </span>
  )
}

function ProviderGroup({ p, gate, first }: { p: ProviderUsageView; gate: GateView | null; first: boolean }) {
  return (
    <div className="min-w-0 flex-1 basis-[300px] flex flex-col gap-2.5">
      <span className="flex min-w-0 items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-ink-muted">
        {p.provider}
        {first && <span className={`normal-case tracking-normal ${chip.neutral}`}>first</span>}
        {gate && <SpawnChip gate={gate} />}
      </span>
      {p.source === 'unavailable' ? (
        <div className={banner.waiting}>
          usage unknown — dispatcher will not spawn on {p.provider}
        </div>
      ) : (
        p.windows.map((w) => <Bullet key={`${w.kind}:${w.scope ?? ''}`} provider={p.provider} w={w} />)
      )}
      {p.source === 'ccusage' && (
        <span className="text-[11px] text-ink-muted">via ccusage · weekly unknown</span>
      )}
    </div>
  )
}

/** A brand is not a capitalised word; anything unlisted gets its first letter raised. */
const NAME: Record<string, string> = { openai: 'OpenAI' }
const optionLabel = (option: string) => NAME[option] ?? option.charAt(0).toUpperCase() + option.slice(1)

const STEP: Record<string, number> = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }

/** Arrows move focus without selecting: a selection here is a write to the
 *  box, so it takes an explicit Space or tap, never one POST per arrow. The
 *  rest (tab stop, Space, checked state) is the native radio group's. */
function moveFocus(event: KeyboardEvent<HTMLDivElement>) {
  const step = STEP[event.key]
  if (!step) return
  event.preventDefault()
  const radios = [...event.currentTarget.querySelectorAll('input')]
  const index = radios.indexOf(event.target as HTMLInputElement)
  radios[(index + step + radios.length) % radios.length]?.focus()
}

/** Which provider the box tries first: `auto`, or one routed provider pinned. */
function PrioritySelector({ priority }: { priority: PriorityView }) {
  const { setPriority, pending, error } = useSetPriority()
  const selected = pending ?? priority.mode
  return (
    <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
      <span aria-hidden="true" className="text-[11px] font-semibold uppercase tracking-wider text-ink-muted">Priority</span>
      <div
        role="radiogroup"
        aria-label="Provider priority"
        aria-busy={pending != null}
        onKeyDown={moveFocus}
        className={`flex gap-1 rounded border bg-surface-raised p-1 ${pending == null ? '' : 'opacity-50'}`}
      >
        {priority.options.map((option) => {
          const label = optionLabel(option)
          return (
            <label
              key={option}
              className="relative rounded-sm px-2.5 py-0.5 text-xs font-medium text-ink-muted hover:text-ink has-checked:bg-ink has-checked:text-surface-raised"
            >
              <input
                type="radio"
                name="priority-mode"
                aria-label={label}
                // The input is the segment's whole hit area, drawn by its label:
                // taps, the focus ring and the checked state stay native. The
                // ring sits inside the control's padding, clear of its neighbours.
                className="absolute inset-0 cursor-pointer appearance-none rounded-sm outline-offset-1"
                checked={option === selected}
                // Not `disabled` while a change is in flight: that would drop keyboard focus.
                onChange={() => { if (pending == null) setPriority(option) }}
              />
              {label}
            </label>
          )
        })}
      </div>
      {error && <p role="alert" className="w-full text-xs text-failed-fg">{error}</p>}
    </div>
  )
}

/** The priority selector heads the panel; under it each provider occupies its
 *  own column in a wrapping row so basis means WIDTH. The panel itself is a
 *  flex item in the board header (min-w-0 flex-1 basis-[340px]). */
export function UsagePanel({ usage }: { usage: UsageView }) {
  return (
    // min-w-0 flex-1 basis-[340px] gives the panel a real width in the header's
    // flex-wrap row; flex-wrap inside lets provider groups sit side by side,
    // each in its own 300px column.
    <div className="min-w-0 flex-1 basis-[340px] flex flex-col gap-3">
      <PrioritySelector priority={usage.priority} />
      <div className="flex flex-wrap gap-3">
        {usage.providers.map((p) => (
          <ProviderGroup
            key={p.provider}
            p={p}
            gate={p.provider === usage.gate.provider ? usage.gate : null}
            first={p.provider === usage.priority.first}
          />
        ))}
      </div>
    </div>
  )
}
