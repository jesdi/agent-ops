import type { GateView, ProviderUsageView, Severity, UsageView, WindowKind, WindowView } from '../lib/api'
import { formatDuration } from '../lib/format'

const FILL: Record<Severity, string> = {
  ok: 'bg-emerald-500', close: 'bg-amber-500', blocked: 'bg-red-500',
}
const STATE: Record<Severity, string> = {
  ok: 'on pace', close: 'close to the limit', blocked: 'over the limit',
}

function pts(x: number): string {
  const v = Math.round(x * 1000) / 10
  return Number.isInteger(v) ? String(v) : v.toFixed(1)
}

/** Per window kind: the row label, and the label under the head line — the
 *  session window shows its cap, a weekly window its headroom. */
const KIND: Record<WindowKind, { label: (w: WindowView) => string; foot: (w: WindowView) => string }> = {
  session: {
    label: () => 'Session · 5h',
    foot: (w) => `cap ${Math.round(w.allowance * 100)}%`,
  },
  weekly: {
    label: (w) => (w.scope ? `Week · ${w.scope}` : 'Week · all'),
    foot: (w) => `headroom ${pts(w.headroom)} pts`,
  },
}

/** Headroom bullet: the hatched band is what the box may have spent by now
 *  (allowance), the fill is what was spent, the gap is headroom — the gate's
 *  actual input. Remaining is spelled out to the right. */
function Bullet({ provider, w }: { provider: string; w: WindowView }) {
  const used = Math.round(w.used * 100)
  const allowed = Math.round(w.allowance * 100)
  const remaining = 100 - used
  const label = KIND[w.kind].label(w)

  // Position the sub-track label centred on the head line, but clamp it so it
  // never overflows the track edges. Below 15%: left-align; above 85%: right-align.
  const headPct = Math.min(100, allowed)
  const footStyle: React.CSSProperties =
    headPct < 15
      ? { left: `${headPct}%` }
      : headPct > 85
      ? { right: `${100 - headPct}%` }
      : { left: `${headPct}%`, transform: 'translateX(-50%)' }

  return (
    // ponytail: minmax(7rem,1fr) ensures the track column never collapses to 0
    // when the grid has no intrinsic width (all progressbar children are absolute).
    <div className="grid grid-cols-[92px_minmax(7rem,1fr)_auto] items-center gap-2.5 text-xs">
      <span className="text-gray-600">
        {label}
        <small className="block text-[11px] text-gray-400">{formatDuration(w.minutes_to_reset * 60)}</small>
      </span>
      <div className="mb-3.5 min-w-0">
        <div
          role="progressbar"
          aria-label={`${provider} ${label} used`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={used}
          aria-valuetext={`${used}% used of ${allowed}% allowed now, ${remaining}% remaining, ${STATE[w.severity]}`}
          className="relative h-3.5 rounded-sm bg-gray-200"
        >
          <div
            className="absolute inset-y-0 left-0 rounded-sm bg-gray-300 [background-image:repeating-linear-gradient(135deg,transparent_0_3px,rgb(0_0_0/0.07)_3px_4px)]"
            style={{ width: `${headPct}%` }}
          />
          <div className={`absolute inset-y-[3px] left-0 rounded-sm ${FILL[w.severity]}`} style={{ width: `${used}%` }} />
          <div className="absolute inset-y-0 border-l-2 border-gray-900" style={{ left: `${headPct}%` }} />
          <span
            className="absolute top-4 whitespace-nowrap text-[11px] text-gray-500"
            style={footStyle}
          >
            {KIND[w.kind].foot(w)}
          </span>
        </div>
      </div>
      <span className="whitespace-nowrap font-semibold text-gray-900">{remaining}% left</span>
    </div>
  )
}

/** The verdict for the policy default model — what an idle box spawns next.
 *  Shown once, on that model's provider; the provider heading names it, so
 *  the chip names only the model. The note says why, on hover. */
function SpawnChip({ gate }: { gate: GateView }) {
  const model = gate.model.slice(gate.model.indexOf('/') + 1)
  const tone = gate.admitted ? 'bg-emerald-100 text-emerald-800' : 'bg-red-100 text-red-800'
  return (
    <span
      title={gate.note}
      className={`min-w-0 truncate rounded-full px-2 py-px text-[11px] font-medium normal-case tracking-normal ${tone}`}
    >
      {`${gate.admitted ? 'will spawn' : 'will not spawn'} ${model}`}
    </span>
  )
}

function ProviderGroup({ p, gate }: { p: ProviderUsageView; gate: GateView | null }) {
  return (
    <div className="min-w-0 flex-1 basis-[300px] flex flex-col gap-2.5">
      <span className="flex min-w-0 items-center gap-2 text-[11px] font-semibold uppercase tracking-wider text-gray-500">
        {p.provider}
        {gate && <SpawnChip gate={gate} />}
      </span>
      {p.source === 'unavailable' ? (
        <div className="rounded border border-amber-400 bg-amber-50 px-3 py-2 text-sm text-amber-800">
          usage unknown — dispatcher will not spawn on {p.provider}
        </div>
      ) : (
        p.windows.map((w) => <Bullet key={`${w.kind}:${w.scope ?? ''}`} provider={p.provider} w={w} />)
      )}
      {p.source === 'ccusage' && (
        <span className="text-[11px] text-gray-400">via ccusage · weekly unknown</span>
      )}
    </div>
  )
}

/** Each provider occupies its own column in a wrapping row so basis means WIDTH.
 *  The panel itself is a flex item in the board header (min-w-0 flex-1 basis-[340px]). */
export function UsagePanel({ usage }: { usage: UsageView }) {
  return (
    // ponytail: min-w-0 flex-1 basis-[340px] gives the panel a real width in the
    // BoardPage header's flex-wrap context; flex-wrap inside lets multiple provider
    // groups sit side by side, each pinned to their own 300px column.
    <div className="min-w-0 flex-1 basis-[340px] flex flex-wrap gap-3">
      {usage.providers.map((p) => (
        <ProviderGroup key={p.provider} p={p} gate={p.provider === usage.gate.provider ? usage.gate : null} />
      ))}
    </div>
  )
}
