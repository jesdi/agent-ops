import type { ReactNode } from 'react'
import { NavLink } from 'react-router'

const links = [
  { to: '/', label: 'Board' },
  { to: '/queue', label: 'Queue' },
  { to: '/failures', label: 'Failures' },
  { to: '/history', label: 'History' },
]

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-gray-50 text-gray-900">
      <nav className="flex gap-4 border-b border-gray-200 bg-white px-4 py-2">
        {links.map((l) => (
          <NavLink
            key={l.to}
            to={l.to}
            className={({ isActive }) =>
              isActive ? 'text-sm font-semibold' : 'text-sm text-gray-500 hover:text-gray-900'
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
