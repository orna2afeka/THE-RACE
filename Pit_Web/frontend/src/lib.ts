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
  // Rounded to milliseconds ONCE, then split. Taking the minute off first and
  // rounding the remainder can print "3:60.000" -- the same defect that showed
  // 239.96 s as "3:60.0" on the pit wall and the public page. Far rarer at this
  // precision, and no less wrong when it lands.
  const ms = Math.round(seconds * 1000);
  const r = (ms % 60000) / 1000;
  return `${Math.floor(ms / 60000)}:${r.toFixed(3).padStart(6, '0')}`;
}

/** Seconds -> M:SS, whole seconds, no fraction. The form the charts use: an
 *  axis label and a hover must read the same, and a tick reading "4:27.404" is
 *  noise at that size. Rounds rather than truncates, so 4:26.8 reads 4:27 like
 *  it does everywhere else. lapTime() keeps the precise figure for the tiles. */
export function lapTimeShort(seconds: Num): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0)
    return MISSING;
  const whole = Math.round(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`;
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

/** Wall-clock hh:mm:ss in the VIEWER's timezone, for marking the instant
 *  something happened rather than how long ago it was. Both are shown
 *  together where it matters: "12:47:31 · 57m 4s ago" survives a screenshot
 *  pasted into a chat an hour later, where "57m ago" alone does not. */
export function clockTime(epochS: number): string {
  const d = new Date(epochS * 1000);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
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
/** How far past a whole-second boundary to land, so a timer that fires a
 *  hair early still renders the NEW second rather than the old one. */
const ALIGN_EPSILON_MS = 15;

/** How many recent server-clock samples to keep. At one push every 2 s that
 *  is about 20 s: long enough to contain an undelayed sample, short enough
 *  to follow a real clock adjustment. */
const OFFSET_WINDOW = 10;

/**
 * The server clock, re-rendering the caller just after a countdown's displayed
 * whole second changes.
 *
 * WHY NOT A 1 s setInterval. That is what this replaced, and measured it was
 * perfectly regular but constantly LATE: an interval starts at an arbitrary
 * moment, so the digit changed 346 ms after it should on one page load and
 * 495 ms on the next, and stayed exactly that late for as long as the page was
 * open. Ticking faster only shrinks that error. Aligning removes it.
 *
 * WHY AN EPOCH. A countdown's digit rolls when ITS elapsed time crosses a whole
 * second, and the race clock and the driver-stint clock count from different
 * instants. One shared timer can be in phase with at most one of them, so each
 * clock passes its own epoch and gets its own schedule. Null means the clock is
 * not running (not started, or held through a stoppage): nothing rolls, so it
 * idles on a slow tick until the epoch changes.
 *
 * The offset is read through a ref rather than listed as a dependency. The
 * socket updates it every 2 s, and restarting the schedule on each push would
 * be churn for no gain -- each delay is recomputed from the latest offset.
 */
export function useAlignedServerNow(clockOffsetMs: number, epochS: number | null): number {
  const [, setTick] = useState(0);
  const offsetRef = useRef(clockOffsetMs);
  offsetRef.current = clockOffsetMs;
  useEffect(() => {
    let id: number | undefined;
    const schedule = () => {
      let delay = 1000;
      if (epochS != null) {
        const sinceMs = Date.now() + offsetRef.current - epochS * 1000;
        const into = ((sinceMs % 1000) + 1000) % 1000;
        delay = 1000 - into + ALIGN_EPSILON_MS;
      }
      id = window.setTimeout(() => { setTick((t) => t + 1); schedule(); }, delay);
    };
    schedule();
    return () => { if (id !== undefined) clearTimeout(id); };
  }, [epochS]);
  return (Date.now() + clockOffsetMs) / 1000;
}

export function useLive(manualLap: number) {
  const [live, setLive] = useState<Live | null>(null);
  const [connected, setConnected] = useState(false);
  const [silentS, setSilentS] = useState(0);
  const [clockOffsetMs, setClockOffsetMs] = useState(0);
  const lastMsg = useRef<number>(Date.now());
  const wsRef = useRef<WebSocket | null>(null);
  const paramsRef = useRef({ manualLap });
  paramsRef.current = { manualLap };
  const offsetsRef = useRef<number[]>([]);

  useEffect(() => {
    let closed = false;
    let timer: number | undefined;
    const connect = () => {
      if (closed) return;
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
      wsRef.current = ws;
      // Parameters go out ONCE on open, and after that only when they
      // actually change. They used to be echoed on every message received,
      // which turned the server's 2 s push into a flat-out loop.
      ws.onopen = () => {
        if (closed) return;
        setConnected(true);
        ws.send(JSON.stringify(paramsRef.current));
      };
      ws.onmessage = (e) => {
        if (closed) return;
        const msg = JSON.parse(e.data) as Live;
        lastMsg.current = Date.now();
        setSilentS(0);
        // Server time vs ours, so the clocks tick against the server's even
        // on a phone whose clock is minutes out.
        //
        // Every sample is biased LOW by however long the message took to
        // arrive and to reach this handler, so the LARGEST of a recent
        // window is the least biased. Taking the newest one let a single
        // message delayed behind a heavy render throw the clocks off by
        // ~200 ms, and the aligned countdowns landed that far out with it.
        const w = offsetsRef.current;
        w.push(msg.ts * 1000 - Date.now());
        if (w.length > OFFSET_WINDOW) w.shift();
        setClockOffsetMs(Math.max(...w));
        setLive(msg);
      };
      ws.onclose = () => { if (!closed) { setConnected(false); timer = window.setTimeout(connect, 2000); } };
      ws.onerror = () => ws.close();
    };
    connect();
    const tick = window.setInterval(() => setSilentS((Date.now() - lastMsg.current) / 1000), 1000);
    return () => { closed = true; if (timer) clearTimeout(timer); clearInterval(tick); wsRef.current?.close(); };
  }, []);

  // The ONLY other thing this sends: the override actually changed. The
  // cadence belongs to the server.
  useEffect(() => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ manualLap }));
  }, [manualLap]);

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
/** `enabled` false stops the poll entirely -- used by panels on a hidden tab,
 *  which have no business spending the main thread on the tab you are on. */
export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = [], enabled = true) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    if (!enabled) return;
    let dead = false;
    const run = () => fnRef.current()
      .then((d) => { if (!dead) { setData(d); setError(null); } })
      .catch((e) => { if (!dead) setError(String(e)); });
    run();
    const id = window.setInterval(run, ms);
    return () => { dead = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, enabled, ...deps]);
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
