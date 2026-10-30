import { useCallback } from 'react'
import { useSearchParams } from 'react-router'
import { useProjects } from '../hooks/useResources'
import type { Column, ProjectView } from '../lib/api'

/** The project of a card whose repository left the config. */
export const NO_PROJECT = ''

export interface ProjectTab { key: string; label: string; paused: boolean }

/** One tab per project in the order received, plus a trailing Other tab
 *  while some card has no project, so such a card never vanishes. */
export function projectTabs(projects: ProjectView[], columns: Column[]): ProjectTab[] {
  const tabs = projects.map((p) => ({ key: p.name, label: p.name, paused: p.paused }))
  const orphaned = columns.some((c) => c.cards.some((card) => card.project === NO_PROJECT))
  return orphaned ? [...tabs, { key: NO_PROJECT, label: 'Other', paused: false }] : tabs
}

/** `columns` holding only `project`'s cards and ghosts. */
export function projectColumns(columns: Column[], project: string): Column[] {
  return columns.map((c) => ({
    ...c,
    cards: c.cards.filter((card) => card.project === project),
    ghosts: c.ghosts.filter((g) => g.project === project),
  }))
}

/** The selected tab: the project named in the URL if it is a tab, else the
 *  first. Only a click writes the URL, so the default board's URL stays clean. */
export function useProjectTab(tabs: ProjectTab[]) {
  const [params, setParams] = useSearchParams()
  const wanted = params.get('project')
  const active = tabs.find((t) => t.key === wanted) ?? tabs[0]
  // Replace, not push, as for the column tab: switching is not a navigation.
  const select = useCallback((key: string) => {
    setParams((p) => { p.set('project', key); return p }, { replace: true })
  }, [setParams])
  return { active, select }
}

/** The board's project tabs and `columns` cut down to the selected one.
 *  Without the project list (still loading or failed) nothing is filtered. */
export function useProjectBoard(columns: Column[]) {
  const query = useProjects()
  const tabs = projectTabs(query.data?.projects ?? [], columns)
  const { active, select } = useProjectTab(tabs)
  return {
    pending: query.isPending,
    tabs, active, select,
    columns: active ? projectColumns(columns, active.key) : columns,
    paused: active?.paused ?? false,
  }
}
