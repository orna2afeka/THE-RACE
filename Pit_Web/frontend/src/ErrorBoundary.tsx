// One tab crashing must not take the pit wall down with it.
//
// Without a boundary, a render error anywhere unmounts the whole React tree
// and the crew is looking at a blank page mid-race. With one per tab, the
// strip, the sidebar and the other tabs keep working and the broken tab says
// what happened and offers a retry.

import { Component, type ReactNode } from 'react';
import { Icon } from './icons';

interface State { error: Error | null }

export class ErrorBoundary extends Component<{ name: string; children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error) {
    // Keep it in the console too — that is where a developer will look.
    console.error(`[${this.props.name}]`, error);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="pill err" role="alert" style={{ flexDirection: 'column', gap: 6 }}>
          <span style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600 }}>
            <Icon name="alert" size={14} />{this.props.name} failed to render
          </span>
          <code style={{ fontSize: 'calc(11px * var(--pit-font-scale))', opacity: 0.85 }}>{this.state.error.message}</code>
          <span>
            <button className="btn" style={{ padding: '4px 10px', fontSize: 'calc(12px * var(--pit-font-scale))' }}
                    onClick={() => this.setState({ error: null })}>Retry</button>
          </span>
        </div>
      );
    }
    return this.props.children;
  }
}
