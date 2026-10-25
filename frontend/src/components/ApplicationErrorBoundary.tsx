import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

/** A rendering error should offer recovery instead of a blank workspace. */
export class ApplicationErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('Workspace render failed', error.name, info.componentStack);
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return <main role="alert" className="min-h-screen grid place-items-center bg-slate-50 p-6">
      <div className="max-w-md rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
        <h1 className="text-lg font-semibold text-slate-900">The workspace could not be displayed</h1>
        <p className="mt-2 text-sm text-slate-600">Reload to recover the page. Saved lead records and background jobs remain on the server.</p>
        <button type="button" className="mt-4 rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold text-white" onClick={() => window.location.reload()}>Reload workspace</button>
      </div>
    </main>;
  }
}
