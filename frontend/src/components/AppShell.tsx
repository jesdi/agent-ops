import type { ReactNode } from 'react'
import { NavLink } from 'react-router'

const links = [
  { to: '/', label: 'Board' },
  { to: '/failures', label: 'Failures' },
  { to: '/history', label: 'History' },
]

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col bg-surface text-ink">
      <nav className="flex gap-4 border-b border-border bg-surface-raised px-4 py-2">
        {links.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            className={({ isActive }) =>
              isActive ? 'text-sm font-semibold' : 'text-sm text-ink-muted hover:text-ink'
            }
          >
            {l.label}
          </NavLink>
        ))}
      </nav>
      {children}
    </div>
  )
}
