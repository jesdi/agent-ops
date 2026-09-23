import { useCallback, useEffect, useSyncExternalStore } from 'react'
import { useSearchParams } from 'react-router'

// Tailwind's md: from here up every column is on screen and the tabs are hidden.
const WIDE = '(min-width: 48rem)'
const subscribe = (onChange: () => void) => {
  const query = window.matchMedia(WIDE)
  query.addEventListener('change', onChange)
  return () => query.removeEventListener('change', onChange)
}
const isWide = () => window.matchMedia(WIDE).matches

/** The phone board's tab: the column named in the URL if it is in `keys`,
 *  else the first of them. While the tabs are shown, the choice is written
 *  back, so the URL always names the column on screen: back and reload land
 *  on it, and a column filling or refilling elsewhere never moves the view.
 *  Desktop URLs stay clean.
 *
 *  Until `settled` (the snapshot carries no ghosts, so Queued may still
 *  fill) the write-back may only fill in a missing key, never overwrite one:
 *  a link to Queued must survive the snapshot. */
export function useActiveColumn(keys: string[], settled: boolean) {
  const [params, setParams] = useSearchParams()
  const tabbed = !useSyncExternalStore(subscribe, isWide)
  const urlColumn = params.get('column')
  const active = keys.find((k) => k === urlColumn) ?? keys[0]
  // Replace, not push: switching tabs is not a navigation back should undo.
  const select = useCallback((key: string) =>
    setParams((p) => { p.set('column', key); return p }, { replace: true }), [setParams])
  useEffect(() => {
    if (tabbed && (settled || urlColumn === null) && active && active !== urlColumn) select(active)
  }, [tabbed, settled, active, urlColumn, select])
  return { active, tabbed, select }
}
