import { Component, type ErrorInfo, type ReactNode } from "react";

type AppErrorBoundaryProps = {
  children: ReactNode;
};

type AppErrorBoundaryState = {
  error: Error | null;
};

export class AppErrorBoundary extends Component<AppErrorBoundaryProps, AppErrorBoundaryState> {
  state: AppErrorBoundaryState = {
    error: null,
  };

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error("Dashboard render failed", error, errorInfo);
  }

  render(): ReactNode {
    if (this.state.error !== null) {
      return <DashboardErrorFallback error={this.state.error} />;
    }

    return this.props.children;
  }
}

type DashboardErrorFallbackProps = {
  error: Error;
};

export function DashboardErrorFallback({ error }: DashboardErrorFallbackProps) {
  return (
    <main className="flex min-h-screen items-center justify-center bg-background px-4 py-10 text-foreground">
      <section className="w-full max-w-xl rounded-lg border border-border bg-card p-6 shadow-sm">
        <p className="text-sm font-medium text-muted-foreground">Codex LB</p>
        <h1 className="mt-2 text-2xl font-semibold">Dashboard failed to render</h1>
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          The page hit a client-side startup error. Reload the page first; reset local settings if this
          browser has a stale or blocked dashboard state.
        </p>
        <pre className="mt-4 max-h-40 overflow-auto rounded-md border border-border bg-muted p-3 text-xs text-muted-foreground">
          {error.message}
        </pre>
        <div className="mt-5 flex flex-wrap gap-2">
          <button
            className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground hover:opacity-90"
            type="button"
            onClick={() => window.location.reload()}
          >
            Reload
          </button>
          <button
            className="rounded-md border border-border px-3 py-2 text-sm font-medium hover:bg-accent"
            type="button"
            onClick={() => {
              try {
                window.localStorage.clear();
              } catch {
                /* Storage reset is best-effort. */
              }
              window.location.reload();
            }}
          >
            Reset local settings
          </button>
        </div>
      </section>
    </main>
  );
}
