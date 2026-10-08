import { Component, Suspense, type ReactNode } from 'react'
import { useLocation } from 'react-router'

type State = { failed: boolean }

class Boundary extends Component<{ children: ReactNode }, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div role="alert" className="p-4 text-sm text-ink">
        <p>this page could not be loaded — the console may have been updated.</p>
        <button
          type="button"
          className="mt-2 rounded border border-border bg-surface-raised px-3 py-1 hover:bg-surface"
          onClick={() => window.location.reload()}
        >
          reload
        </button>
      </div>
    )
  }
}

/** Wraps a lazy page: its loading state and its load failure. */
export function PageErrorBoundary({ children }: { children: ReactNode }) {
  return (
    <Boundary key={useLocation().pathname}>
      <Suspense fallback={<p className="p-4 text-ink-muted">loading page…</p>}>{children}</Suspense>
    </Boundary>
  )
}
