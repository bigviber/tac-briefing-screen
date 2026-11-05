import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  label: string;
  children: ReactNode;
}
interface State {
  error: Error | null;
}

/** Contains render errors to a single pane so the rest of the workbench stays usable. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error(`[${this.props.label}]`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex h-full flex-col items-start gap-2 p-4 text-xs" role="alert">
        <div className="font-medium text-bad">{this.props.label} crashed</div>
        <pre className="max-w-full overflow-auto whitespace-pre-wrap font-mono text-muted">{this.state.error.message}</pre>
        <button className="btn btn-sm" onClick={() => this.setState({ error: null })}>
          Reload pane
        </button>
      </div>
    );
  }
}
