// Toasts — the acknowledgement a button needs.
//
// "Sent" appearing beside the button for a moment is what Streamlit's st.toast
// gave the crew, and a button that does nothing visible when pressed gets
// pressed again. Tiny on purpose: a module-level store and one host component.

import { useEffect, useState } from 'react';
import { Icon } from './icons';

export type ToastKind = 'ok' | 'err' | 'info';
interface Toast { id: number; kind: ToastKind; text: string }

let seq = 0;
let toasts: Toast[] = [];
const listeners = new Set<() => void>();

export function toast(text: string, kind: ToastKind = 'ok') {
  const id = ++seq;
  toasts = [...toasts, { id, kind, text }];
  listeners.forEach((l) => l());
  window.setTimeout(() => {
    toasts = toasts.filter((t) => t.id !== id);
    listeners.forEach((l) => l());
  }, kind === 'err' ? 6000 : 3200);
}

export function ToastHost() {
  const [, bump] = useState(0);
  useEffect(() => {
    const l = () => bump((n) => n + 1);
    listeners.add(l);
    return () => { listeners.delete(l); };
  }, []);
  if (!toasts.length) return null;
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`}>
          <Icon name={t.kind === 'ok' ? 'check' : t.kind === 'err' ? 'alert' : 'radio'} size={14} />
          {t.text}
        </div>
      ))}
    </div>
  );
}
