// Formatting and data access.
//
// FORMATTING RULE: every numeric readout goes through fmt(). A missing value
// can never reach the screen disguised as a real one — a "0" battery
// temperature or "0 V" pack is a lie the pit wall acts on.

import { useEffect, useRef, useState, type RefObject } from 'react';
import * as Plotly from 'plotly.js-dist-min';
import type { Config, Live, Num } from './types';

export const MISSING = '—';

/** Python-style format specs, limited to the handful the catalogue uses
 *  (".1f", "+.1f", ".0f", ".2f", "d"). The SPEC is chosen in Python and
 *  travels with the metric; this only renders it. */
export function fmt(value: Num | string | undefined, spec = '.0f'): string {
  if (value === null || value === undefined) return MISSING;
  if (typeof value === 'string') return value || MISSING;
  if (!Number.isFinite(value)) return MISSING;
  const plus = spec.startsWith('+');
  const s = plus ? spec.slice(1) : spec;
  let out: string;
  if (s === 'd') out = String(Math.round(value));
  else {
    const m = /^\.(\d+)f$/.exec(s);
    out = value.toFixed(m ? Number(m[1]) : 0);
  }
  return plus && value >= 0 ? `+${out}` : out;
}

export function fmtStat(value: Num): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return MISSING;
  return Math.abs(value) >= 1000
    ? value.toLocaleString(undefined, { maximumFractionDigits: 0 })
    : value.toFixed(1);
}

/** Seconds -> M:SS.mmm. Em dash for null so an uncompleted lap is visibly
 *  absent rather than showing 0:00.000, which looks like a real lap time. */
export function lapTime(seconds: Num): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0)
    return MISSING;
  const m = Math.floor(seconds / 60);
  return `${m}:${(seconds - m * 60).toFixed(3).padStart(6, '0')}`;
}

/** Data age in units a human reads at a glance. */
export function ageText(seconds: Num): string {
  if (seconds === null) return MISSING;
  const s = Math.floor(seconds);
  if (s < 90) return `${s}s`;
  const m = Math.floor(s / 60), sec = s % 60;
  if (m < 60) return `${m}m ${sec}s`;
  const h = Math.floor(m / 60), mm = m % 60;
  if (h < 24) return `${h}h ${mm}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

export function hms(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}

/** hh:mm:ss with a leading sign once the value goes negative, for a
 *  countdown that keeps counting after it passes zero. */
export function hmsSigned(totalSeconds: number): string {
  const neg = totalSeconds < 0;
  return (neg ? '+' : '') + hms(Math.abs(totalSeconds));
}

/** A local wall-clock value for <input type="datetime-local">. NOT
 *  toISOString(): that is UTC, and the input reads as local, so the default
 *  would be off by the timezone offset and the submit would compound it. */
export function localInput(epochS: number): string {
  const d = new Date(epochS * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function tierClass(tier?: string): string {
  return tier === 'warning' || tier === 'critical' ? tier : '';
}

// --------------------------------------------------------------------------- //
export async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path);
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

export async function postJSON<T>(path: string, body: unknown, method = 'POST'): Promise<T> {
  const r = await fetch(path, {
    method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.ok) {
    let detail = `HTTP ${r.status}`;
    try { const j = await r.json(); if (j?.detail) detail = String(j.detail); }
    catch { /* not JSON; the status is all we have */ }
    throw new Error(detail);
  }
  return r.json() as Promise<T>;
}

/** Live fast tier over the WebSocket — 2 s, pushed rather than polled.
 *
 *  Reconnects on drop. Also reports how long the socket has been SILENT, and
 *  the server↔client clock offset: a dashboard that quietly stops updating is
 *  worse than one that says it is offline, and the server's `fresh` flag
 *  cannot tell you that, because it stops arriving too. */
export function useLive(manualLap: number, rivalLaps: number) {
  const [live, setLive] = useState<Live | null>(null);
  const [connected, setConnected] = useState(false);
  const [silentS, setSilentS] = useState(0);
  const [clockOffsetMs, setClockOffsetMs] = useState(0);
  const lastMsg = useRef<number>(Date.now());
  const wsRef = useRef<WebSocket | null>(null);
  const paramsRef = useRef({ manualLap, rivalLaps });
  paramsRef.current = { manualLap, rivalLaps };

  useEffect(() => {
    let closed = false;
    let timer: number | undefined;
    const connect = () => {
      if (closed) return;
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
      wsRef.current = ws;
      ws.onopen = () => !closed && setConnected(true);
      ws.onmessage = (e) => {
        if (closed) return;
        const msg = JSON.parse(e.data) as Live;
        lastMsg.current = Date.now();
        setSilentS(0);
        // Server time vs ours, so the race clock ticks against the server's
        // clock even on a phone whose clock is minutes out.
        setClockOffsetMs(msg.ts * 1000 - Date.now());
        setLive(msg);
        ws.send(JSON.stringify(paramsRef.current));
      };
      ws.onclose = () => { if (!closed) { setConnected(false); timer = window.setTimeout(connect, 2000); } };
      ws.onerror = () => ws.close();
    };
    connect();
    const tick = window.setInterval(() => setSilentS((Date.now() - lastMsg.current) / 1000), 1000);
    return () => { closed = true; if (timer) clearTimeout(timer); clearInterval(tick); wsRef.current?.close(); };
  }, []);

  return { live, connected, silentS, clockOffsetMs };
}

export function useConfig() {
  const [config, setConfig] = useState<Config | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    getJSON<Config>('/api/config').then(setConfig).catch((e) => setError(String(e)));
  }, []);
  return { config, error };
}

/** Re-runs `fn` every `ms`, and once immediately. The heavy tier: 10 s. */
export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    let dead = false;
    const run = () => fnRef.current()
      .then((d) => { if (!dead) { setData(d); setError(null); } })
      .catch((e) => { if (!dead) setError(String(e)); });
    run();
    const id = window.setInterval(run, ms);
    return () => { dead = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, ...deps]);
  return { data, error };
}

/** Per-browser preference. `valid` rejects a stale stored value (a renamed
 *  tab, say) so a leftover from an older build cannot leave the page blank. */
export function useStored<T>(key: string, initial: T, valid?: (v: T) => boolean): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      if (raw === null) return initial;
      const parsed = JSON.parse(raw) as T;
      return valid && !valid(parsed) ? initial : parsed;
    } catch { return initial; }
  });
  const set = (next: T) => {
    setV(next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* private mode */ }
  };
  return [v, set];
}

/** Keep a Plotly chart sized to its container.
 *
 *  Plotly's `responsive: true` only listens to WINDOW resize. A container that
 *  changes width on its own — the sidebar collapsing, a hidden tab becoming
 *  visible — leaves the chart at its old size (or, if it was first drawn while
 *  hidden, at Plotly's 700px default) until the window happens to resize. */
export function useResizePlot(ref: RefObject<HTMLDivElement | null>, visible = true) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let raf = 0;
    const resize = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        if (el.offsetWidth > 0 && (el as unknown as { data?: unknown[] }).data) {
          void Plotly.Plots.resize(el);
        }
      });
    };
    const ro = new ResizeObserver(resize);
    ro.observe(el.parentElement ?? el);
    if (visible) resize();
    return () => { ro.disconnect(); cancelAnimationFrame(raf); };
  }, [ref, visible]);
}
