import type { ReactNode } from 'react'
import { NavLink } from 'react-router'
import { useExamDebts } from '../hooks/useResources'

const links = [
  { to: '/', label: 'Board' },
  { to: '/failures', label: 'Failures' },
  { to: '/exam-debt', label: 'Exam debt' },
  { to: '/history', label: 'History' },
  { to: '/settings', label: 'Settings' },
  { to: '/onboarding', label: 'Onboarding' },
]

export function AppShell({ children }: { children: ReactNode }) {
  const debt = useExamDebts().data?.length
  return (
    <div className="flex min-h-dvh flex-col bg-surface text-ink">
      <nav className="flex flex-wrap gap-x-4 gap-y-2 border-b border-border bg-surface-raised px-4 py-2">
        {links.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            className={({ isActive }) =>
              isActive ? 'text-sm font-semibold' : 'text-sm text-ink-muted hover:text-ink'
            }
          >
            {l.label}
            {l.to === '/exam-debt' && debt ? ` ${debt}` : ''}
          </NavLink>
        ))}
        {/* A static page (public/task-states.html), outside the router: a
            plain link, so the browser loads it. */}
        <a href="/task-states.html" className="ml-auto text-sm text-ink-muted hover:text-ink">
          Task states
        </a>
      </nav>
      {children}
    </div>
  )
}
