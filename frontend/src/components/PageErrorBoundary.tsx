import { Component, type ReactNode } from 'react'

type State = { failed: boolean }

export class PageErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div role="alert" className="p-4 text-sm text-gray-700">
        <p>this page could not be loaded — the console may have been updated.</p>
        <button
          type="button"
          className="mt-2 rounded border border-gray-300 bg-white px-3 py-1 hover:bg-gray-100"
          onClick={() => window.location.reload()}
        >
          reload
        </button>
      </div>
    )
  }
}
