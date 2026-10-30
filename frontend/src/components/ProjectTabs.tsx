import { useRef, type KeyboardEvent } from 'react'
import { banner, chip } from '../lib/tone'
import { TAB_STEP } from './tabKeys'
import type { ProjectTab } from './useProjectTab'

export const PROJECT_PANEL_ID = 'project-panel'

/** The board's project picker, on every width: one pill per project. The row
 *  scrolls sideways on its own when the names outgrow it, never the page.
 *  Selection follows focus; arrows, Home and End move it. */
export function ProjectTabs({ tabs, active, onSelect }: {
  tabs: ProjectTab[]
  active: ProjectTab | undefined
  onSelect: (key: string) => void
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])
  if (tabs.length === 0) return null

  const onKeyDown = (event: KeyboardEvent) => {
    const step = TAB_STEP[event.key]
    if (!step) return
    event.preventDefault()
    const index = step(Math.max(tabs.indexOf(active!), 0), tabs.length - 1)
    onSelect(tabs[index]!.key)
    refs.current[index]?.focus()
  }

  return (
    <div
      role="tablist"
      aria-label="Projects"
      onKeyDown={onKeyDown}
      className="-mx-4 flex gap-1 overflow-x-auto px-4"
    >
      {tabs.map((tab, i) => {
        const selected = tab === active
        return (
          <button
            key={tab.key}
            ref={(el) => { refs.current[i] = el }}
            type="button"
            role="tab"
            id={`project-tab-${i}`}
            aria-selected={selected}
            aria-controls={PROJECT_PANEL_ID}
            tabIndex={selected ? 0 : -1}
            onClick={() => onSelect(tab.key)}
            className={`flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full px-3 py-1 text-sm ${
              selected ? 'bg-ink font-semibold text-surface' : 'text-ink-muted hover:bg-ink/5 hover:text-ink'}`}
          >
            {tab.label}
            {tab.paused && <span className={chip.waiting}>Paused</span>}
          </button>
        )
      })}
    </div>
  )
}

export function PausedNotice({ project }: { project: string }) {
  return (
    <p role="status" className={banner.waiting}>
      {project} is paused: new issues are not claimed, and work already claimed carries on.
    </p>
  )
}
