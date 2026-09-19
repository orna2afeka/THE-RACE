// History — the reason for the rewrite.
//
// In Streamlit every refresh destroyed the chart and any zoom with it. Here the
// chart is a ref, newPlot runs once per (metric set, window), and every new
// sample arrives via extendTraces — so zoom, pan and hover simply persist.
//
// Beyond that, three behaviours a pit engineer will feel:
//   * the zoom SURVIVES adding or removing a metric, normalising, or a theme
//     change — anything that redraws the same window re-applies the range.
//     Only picking a different window resets it, because that is a new range.
//   * a redraw never blanks the chart: the old plot stays until the new data
//     is ready, then newPlot swaps it in place.
//   * over a 24 h race the appended points would pile up on an SVG chart, so
//     past a ceiling the window is silently re-thinned — zoom preserved.
//   * NOTHING HERE RUNS WHILE THE TAB IS HIDDEN. App keeps this tab mounted so
//     the zoom survives a switch, and everything on it used to keep polling
//     and redrawing in the background -- measured, 31 s of every 60 blocked
//     on Driver Telemetry. Now opening the tab reloads the latest window.

import { useEffect, useRef, useState } from 'react';
import * as Plotly from 'plotly.js-dist-min';
import { Disclosure, Pill, SectionTitle } from '../components';
import { Icon } from '../icons';
import { MISSING, ageText, fmtStat, getJSON, lapTime, lapTimeShort, postJSON, usePoll, useResizePlot, useStored } from '../lib';
import { toast } from '../toast';
import { config as plotConfig, layoutBase, theme } from '../plotly-theme';
import type { Config, HistoryResponse, Num, StatRow } from '../types';

/** `fresh` and `age` are the same feed-freshness readings behind the app-bar
 *  STALE badge, so History cannot call itself live while the bar says stale. */
interface Props { config: Config; dark: boolean; visible: boolean; fresh: boolean; age: Num }

interface AppendMsg {
  type: 'append';
  t: (string | null)[];
  series: Record<string, Num[]>;
  cursor: number;
}

/** Past this many points on the SVG chart, reload the window (which re-thins
 *  it evenly). At 1 Hz that is ~8 hours of appends on a 15-minute window. */
const POINT_CEILING = 30000;

/** Milliseconds for a Plotly axis value, which is a number or a date string.
 *  Plotly hands ranges back as "2026-09-19 16:14:20.314" — a space, not a T,
 *  which not every browser's Date parses. */
const axisMs = (v: unknown): number =>
  typeof v === 'number' ? v : Date.parse(String(v).replace(' ', 'T'));

/** The held zoom, but only while it still frames the data it was taken on;
 *  null hands the axis back to autorange. See where it is called. */
function fitsData(range: unknown[] | null, t: (string | null)[]): unknown[] | null {
  if (!range || t.length === 0) return null;
  const first = axisMs(t[0]), last = axisMs(t[t.length - 1]);
  const lo = axisMs(range[0]), hi = axisMs(range[1]);
  if (![first, last, lo, hi].every(Number.isFinite)) return null;
  // 5 % of the window either side, and never less than a minute, so a nudge
  // past the live end is still a view of this data and keeps its zoom.
  const pad = Math.max((last - first) * 0.05, 60_000);
  return lo >= first - pad && hi <= last + pad ? range : null;
}

export default function History({ config, dark, visible, fresh, age }: Props) {
  const [selected, setSelected] = useState<string[]>(config.historyDefaultMetrics);
  const [windowLabel, setWindowLabel] = useState('15 min');
  const [normalize, setNormalize] = useState(false);
  const [paused, setPaused] = useState(false);
  const [status, setStatus] = useState<{ count: number; sampled: number; total: number; thinned: boolean; tz: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [empty, setEmpty] = useState(false);
  const [appended, setAppended] = useState(0);
  const [refused, setRefused] = useState<string[]>([]);
  const [reloadKey, setReloadKey] = useState(0);

  const holder = useRef<HTMLDivElement | null>(null);
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const pointsRef = useRef(0);
  const lastWindowRef = useRef(windowLabel);

  useResizePlot(holder, visible);

  // Drag pans by default: on a live chart the usual gesture is sliding back
  // along time, not drawing a box. Remembered per browser. Applied with
  // relayout, so switching never redraws the chart or loses the view.
  const [dragmode, setDragmode] = useStored<'pan' | 'zoom'>('pit.histDrag', 'pan',
    (v) => v === 'pan' || v === 'zoom');
  const dragRef = useRef(dragmode);
  dragRef.current = dragmode;
  useEffect(() => {
    const g = holder.current as unknown as { data?: unknown[] } | null;
    if (g && g.data) void Plotly.relayout(holder.current as HTMLDivElement, { dragmode });
  }, [dragmode]);

  // Nothing goes into the graph while the feed is stale. When it comes back,
  // reload the window rather than trusting whatever arrived across the gap:
  // a collector catching up after a dropout lands in one piece that way.
  const freshRef = useRef(fresh);
  freshRef.current = fresh;
  const wasFresh = useRef(fresh);
  useEffect(() => {
    if (fresh && !wasFresh.current && visible) setReloadKey((k) => k + 1);
    wasFresh.current = fresh;
  }, [fresh, visible]);

  const minutes = config.historyWindows[windowLabel];
  const chosen = config.metrics.filter((m) => selected.includes(m.key));
  const qs = `metrics=${encodeURIComponent(selected.join(','))}` + (minutes ? `&minutes=${minutes}` : '');

  const { data: stats } = usePoll(
    () => getJSON<{ stats: StatRow[] }>(`/api/history/stats?${qs}`), 10000, [qs], visible);

  // Purge only on true unmount — never between reloads, so the old chart stays
  // visible while the next window loads.
  useEffect(() => () => { if (holder.current) Plotly.purge(holder.current); }, []);

  // ---- newPlot per (metrics, window), then appends forever ---------------- //
  useEffect(() => {
    // Hidden: no fetch, no socket, no draw. Shown again: reload the latest
    // window, keeping the zoom (same window, so keepRange below applies).
    if (!selected.length || !visible) return;
    let cancelled = false;
    let ws: WebSocket | null = null;
    const host = holder.current;
    setAppended(0);
    setLoading(true);

    // Remember the user's zoom if this redraw is for the SAME window.
    const sameWindow = lastWindowRef.current === windowLabel;
    lastWindowRef.current = windowLabel;
    const prevAxis = (host as Plotly.PlotlyDiv | null)?.layout?.xaxis;
    const heldRange = sameWindow && prevAxis?.autorange === false && Array.isArray(prevAxis.range)
      ? prevAxis.range : null;

    (async () => {
      const hist = await getJSON<HistoryResponse>(`/api/history?${qs}`);
      if (cancelled || !host) return;
      pointsRef.current = hist.count;
      setEmpty(hist.count === 0);

      // A KEPT ZOOM MUST STILL BE A VIEW OF THIS DATA. Plotly sets
      // autorange=false after any drag, and the range then survived every
      // later redraw of the same window -- so an afternoon's zoom-out, or a
      // pan off the end while the car was stopped, left the window's three
      // hours of telemetry as a sliver at one edge of an axis a day wide,
      // every time the tab was opened after that. Nothing on the page said
      // why, and the window buttons appeared not to work.
      //
      // Honouring it only when it sits INSIDE the data keeps the promise that
      // matters -- a zoom survives adding a metric, normalising, a theme
      // change, a reload -- and drops it exactly when it has stopped
      // describing what is on screen. A little padding, because panning a
      // fraction past the live end to watch new points arrive is normal.
      const keepRange = fitsData(heldRange, hist.t);

      const units: string[] = [];
      chosen.forEach((m) => { if (!units.includes(m.unit)) units.push(m.unit); });
      const plotted = normalize ? units : units.slice(0, 2);
      const refusedUnits = normalize ? [] : units.slice(2);
      setRefused(refusedUnits);

      const t = theme(dark);
      const traces = chosen.filter((m) => !refusedUnits.includes(m.unit)).map((m) => {
        const raw = hist.series[m.key] ?? [];
        let y: Num[] = raw;
        if (normalize) {
          const clean = raw.filter((v): v is number => v !== null);
          const lo = Math.min(...clean), hi = Math.max(...clean), span = hi - lo;
          y = raw.map((v) => (v === null ? null : span ? ((v - lo) / span) * 100 : 50));
        }
        return {
          // SVG scatter, NOT scattergl: WebGL silently ignores rangebreaks.
          type: 'scatter', mode: 'lines',
          name: `${m.label} (${m.unit})`,
          x: hist.t, y,
          // Break the line at a missing reading. NEVER plot the gap as 0.
          connectgaps: false,
          line: { color: m.color, width: 2, shape: 'linear' },
          hovertemplate: `%{y:.2f} ${m.unit}<extra>${m.label}</extra>`,
          yaxis: normalize || plotted.indexOf(m.unit) === 0 ? 'y' : 'y2',
        };
      });

      // Direct end-labels as a SECONDARY identity channel: two palette hexes
      // (Motor Temp / Controller Temp) are ΔE 6.6 apart and share the °C axis.
      const annotations = traces.map((tr) => {
        const ys = tr.y as Num[];
        let i = ys.length - 1;
        while (i >= 0 && ys[i] === null) i--;
        if (i < 0) return null;
        return {
          x: (tr.x as (string | null)[])[i], y: ys[i], xref: 'x', yref: tr.yaxis === 'y2' ? 'y2' : 'y',
          text: tr.name.replace(/ \(.*\)$/, ''), showarrow: false,
          xanchor: 'left', xshift: 6, font: { size: 10, color: t.ink2 },
          bgcolor: dark ? 'rgba(20,25,36,0.85)' : 'rgba(255,255,255,0.85)', borderpad: 2,
        };
      }).filter(Boolean);

      // NEUTRAL LINE for the pedal trace. The one-pedal control regenerates
      // below config.pedal.neutralMv and accelerates above it, so a raw pedal
      // trace without that datum is just a wandering voltage — the line is what
      // turns it into "lifting here, on the power there".
      //
      // Only when the pedal is on a real millivolt axis: normalising rescales
      // every trace to % of its own range, and a fixed millivolt value has no
      // meaning on that axis. Matched by UNIT, so it follows the metric whether
      // it landed on the left or the right axis.
      // config.pedal is optional for the same reason the lap fields are: an API
      // process older than this page does not send it. No neutral point means
      // no line — the pedal trace still draws, just without its datum.
      const neutralMv = config.pedal?.neutralMv ?? null;
      const pedalAxis = normalize || neutralMv === null
        ? null
        : (traces.find((tr) => tr.name.endsWith('(mV)'))?.yaxis ?? null);
      const shapes = pedalAxis ? [{
        type: 'line' as const, xref: 'paper' as const, x0: 0, x1: 1,
        yref: (pedalAxis === 'y2' ? 'y2' : 'y') as 'y' | 'y2',
        y0: neutralMv, y1: neutralMv,
        line: { color: t.ink3, width: 1, dash: 'dot' as const },
      }] : [];
      // Its own array rather than a push onto `annotations`: that one is typed
      // from the end-label map above, whose x is a timestamp string. This one
      // is anchored to the paper's left edge, so its x is a number.
      const pedalNote = pedalAxis ? [{
        x: 0, y: neutralMv, xref: 'paper', yref: pedalAxis === 'y2' ? 'y2' : 'y',
        text: 'neutral — regen below, power above', showarrow: false,
        xanchor: 'left', xshift: 4, yshift: 8,
        font: { size: 10, color: t.ink3 },
        bgcolor: dark ? 'rgba(20,25,36,0.85)' : 'rgba(255,255,255,0.85)', borderpad: 2,
      }] : [];

      const base = layoutBase(dark, 470);
      await Plotly.newPlot(host, traces, {
        shapes,
        ...base,
        margin: { l: 56, r: 64, t: 36, b: 40 },
        xaxis: { ...base.xaxis, type: 'date', rangebreaks: hist.rangebreaks,
                 ...(keepRange ? { range: keepRange, autorange: false } : {}) },
        yaxis: { ...base.yaxis, title: { ...base.yaxis.title, text: normalize ? '% of range' : (plotted[0] ?? '') } },
        yaxis2: {
          ...base.yaxis, gridcolor: 'rgba(0,0,0,0)', overlaying: 'y', side: 'right',
          title: { ...base.yaxis.title, text: normalize ? '' : (plotted[1] ?? '') },
        },
        showlegend: traces.length > 1,
        dragmode: dragRef.current,
        annotations: [...annotations, ...pedalNote],
        // Keyed on the WINDOW, not a constant. A constant asked Plotly to keep
        // the viewer's zoom across every redraw, and whether it honoured that
        // on a window change depended on the new window's range breaks: the
        // demo store reset, the replay store kept a 15-minute zoom inside a
        // 1-hour window. Same window keeps the zoom; a new window is a new
        // range, which is what the note at the top of this file promises.
        uirevision: windowLabel,
      }, plotConfig);
      if (cancelled) return;
      setStatus({ count: hist.count, sampled: hist.sampled, total: hist.total, thinned: hist.downsampled, tz: hist.tz });
      setLoading(false);

      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      ws = new WebSocket(`${proto}://${location.host}/ws/history`);
      ws.onopen = () => ws?.send(JSON.stringify({ metrics: selected, cursor: hist.cursor }));
      ws.onmessage = (e) => {
        if (cancelled || !host || pausedRef.current || !freshRef.current) return;
        const msg = JSON.parse(e.data) as AppendMsg;
        if (msg.type !== 'append' || !msg.t.length) return;
        const idx: number[] = [], xs: unknown[][] = [], ys: unknown[][] = [];
        traces.forEach((tr, i) => {
          const key = chosen.find((m) => `${m.label} (${m.unit})` === tr.name)?.key;
          if (!key) return;
          idx.push(i); xs.push(msg.t);
          // `?? 0` here would be the single most damaging line in this app.
          ys.push(msg.series[key] ?? []);
        });
        if (!idx.length) return;
        void Plotly.extendTraces(host, { x: xs, y: ys }, idx);
        pointsRef.current += msg.t.length;
        setAppended((n) => n + msg.t.length);
        setEmpty(false);
        // Ceiling reached: re-thin the window in place. Same window, so the
        // zoom is carried across the redraw.
        if (pointsRef.current > POINT_CEILING) setReloadKey((k) => k + 1);
      };
    })().catch(() => !cancelled && setLoading(false));

    return () => { cancelled = true; ws?.close(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qs, normalize, dark, reloadKey, visible]);

  const toggle = (key: string) =>
    setSelected((s) => (s.includes(key) ? s.filter((k) => k !== key) : [...s, key]));
  const exportUrl = (style: string) => `/api/export/history.csv?${qs}&style=${style}`;

  return (
    <>
      <div className="toolbar">
        <div className="chips">
          <span className="lbl">Window</span>
          {Object.keys(config.historyWindows).map((w) => (
            <button key={w} className="chip" aria-pressed={w === windowLabel} onClick={() => setWindowLabel(w)}>{w}</button>
          ))}
        </div>
        <div className="chips">
          <span className="lbl">Scale</span>
          <button className="chip" aria-pressed={!normalize} onClick={() => setNormalize(false)}>Raw units</button>
          <button className="chip" aria-pressed={normalize} onClick={() => setNormalize(true)}>Normalize</button>
        </div>
        <div className="chips">
          <span className="lbl">Drag</span>
          <button className="chip" aria-pressed={dragmode === 'pan'} onClick={() => setDragmode('pan')}>Pan</button>
          <button className="chip" aria-pressed={dragmode === 'zoom'} onClick={() => setDragmode('zoom')}>Zoom</button>
        </div>
      </div>
      <div className="chips" style={{ marginBottom: 10 }}>
        <span className="lbl">Metrics</span>
        {config.metrics.map((m) => (
          <button key={m.key} className="chip metric" aria-pressed={selected.includes(m.key)}
                  onClick={() => toggle(m.key)} style={{ ['--sw' as string]: m.color }}>
            <span className="sw" />{m.label}
          </button>
        ))}
      </div>

      <div className={`hist-badge ${paused || !fresh ? 'frozen' : 'live'}`}>
        <span className="dot" />
        <span>{paused
          ? 'PAUSED — chart holding still · Resume reloads the latest'
          : !fresh
            ? (age === null
              ? 'NO DATA — nothing received from the car · showing stored history'
              : `STALE — no new data for ${ageText(age)} · showing stored history, nothing is being added`)
            : 'LIVE — appending every 10 s · zoom and pan survive'}</span>
        <span className="meta">
          {loading ? 'loading…' : status && (
            status.thinned
              ? <><b>{status.sampled.toLocaleString()}</b> samples, thinned to <b>{status.count.toLocaleString()}</b> for drawing</>
              : <><b>{status.count.toLocaleString()}</b> samples</>
          )}
          {status && <> · <b>{status.total.toLocaleString()}</b> stored</>}
          {appended > 0 && <> · <b>+{appended.toLocaleString()}</b> appended</>}
          {status?.tz && <> · times in {status.tz}</>}
        </span>
        <span style={{ marginLeft: 'auto' }} className="btnrow">
          <button className="btn" onClick={() => {
            // Appends that land while paused are dropped, so resuming reloads
            // the window rather than leaving a hole where the pause was.
            if (paused) setReloadKey((k) => k + 1);
            setPaused((p) => !p);
          }}>
            <Icon name={paused ? 'play' : 'pause'} size={12} />{paused ? 'Resume' : 'Pause'}
          </button>
          <a className="btn" href={exportUrl('data')}><Icon name="download" size={12} />CSV</a>
          <a className="btn" href={exportUrl('report')}><Icon name="download" size={12} />CSV report</a>
        </span>
      </div>

      {!selected.length && <Pill kind="info">Pick at least one metric above.</Pill>}
      {empty && !loading && (
        <Pill kind="info">No telemetry in this range. Widen the window, or start collector.py — new samples will appear here as they arrive.</Pill>
      )}
      {refused.length > 0 && (
        <Pill kind="warn">
          {refused.join(', ')} not plotted — two y-axes are already in use, and a third unit
          on someone else's scale would be a lie. Switch to Normalize to see them together.
        </Pill>
      )}

      <div className={`chart${loading ? ' loading' : ''}`}><div ref={holder} className="chart-inner" /></div>

      <SectionTitle icon="table" title="Statistics over this range" right="nulls skipped, never averaged as 0" />
      <div className="grid g4">
        {(stats?.stats ?? []).map((s) => (
          <div key={s.key} className="tile stat" style={{ ['--sw' as string]: s.color }}>
            <div className="tile-label"><span className="sw" />{s.label}</div>
            <div className="now">{fmtStat(s.now)}<small>{s.unit}</small></div>
            <div className="mmm">
              <span>min <b>{fmtStat(s.min)}</b></span>
              <span>avg <b>{fmtStat(s.avg)}</b></span>
              <span>max <b>{fmtStat(s.max)}</b></span>
            </div>
            <div className="foot">
              {s.samples.toLocaleString()} samples{s.missing ? ` · ${s.missing.toLocaleString()} missing` : ''}
            </div>
          </div>
        ))}
      </div>

      {/* `?? []`: the frontend is reloaded with a click and the server is
          restarted by hand, so for a while a NEW page talks to an OLD server
          whose /api/config has no driver list. That must cost the dropdown its
          names, not the whole History tab its render. */}
      <LapCharts dark={dark} visible={visible} drivers={config.drivers ?? []} />
      <RecentSamples config={config} visible={visible} />
      <Faults visible={visible} />
    </>
  );
}

// --------------------------------------------------------------------------- //
interface LapsResp {
  /** kind is the CAR's verdict; null from a car that predates it. */
  laps: { lap: number; driver: string | null; key: string; driverEdited: boolean; energyWh: Num; lapTimeS: Num;
          distanceM: Num; kind: string | null; flags: string[];
          source: string | null; stoppedS: Num; started: string | null }[];
  summary: { count: number; flyingCount: number; bestS: Num; avgS: Num; avgWh: Num };
}

// Flying laps keep the chart's own colours; everything else is drawn muted, so
// a 15-minute in-lap reads as "pit stop" and not as the race falling apart.
const KIND_NAME: Record<string, string> = {
  flying: 'flying', in: 'in-lap', out: 'out-lap', in_out: 'in + out',
  start: 'not from the line', suspect: 'suspect',
};
const MUTED = '#8a93a6';
const counts = (kind: string | null) => kind === null || kind === 'flying';

function LapCharts({ dark, visible, drivers }: { dark: boolean; visible: boolean; drivers: string[] }) {
  // `edits` re-runs the poll the moment a driver is changed by hand, so the
  // table answers the click instead of up to ten seconds later.
  const [edits, setEdits] = useState(0);
  const { data } = usePoll(() => getJSON<LapsResp>('/api/laps'), 10000, [edits], visible);
  const eRef = useRef<HTMLDivElement | null>(null);
  const tRef = useRef<HTMLDivElement | null>(null);
  useResizePlot(eRef, visible);
  useResizePlot(tRef, visible);

  // Redraw only when the laps actually changed. The poll hands back a new
  // object every 10 s, and redrawing identical data on that cadence is what
  // froze the page.
  // ?? [] on EVERY read of an API array, here and below. The pit runs the API
  // and the page as separate processes, and restarting the browser is not the
  // same act as restarting "Pit Web": a page newer than its backend gets JSON
  // without the fields it expects, and `data.laps.length` on a missing array
  // took the whole History tab down with "Cannot read properties of undefined".
  // A tab that quietly shows nothing is recoverable; one that crashes is not.
  const laps = data?.laps ?? [];
  // Same reasoning for the summary block: an older API sends no `summary` at
  // all, and reading through it crashed the tab rather than leaving four
  // readouts blank. undefined !== undefined is false, so a missing summary
  // simply hides the flying-lap note instead of claiming every lap was flying.
  const summary = data?.summary;
  const someNotFlying = summary != null && summary.flyingCount !== summary.count;
  const last = laps.length ? laps[laps.length - 1] : null;
  const sig = data ? `${laps.length}:${last?.lap}:${last?.energyWh}:${last?.lapTimeS}:${last?.kind}`
    // A rename lands on every lap of the stint at once, so the drivers
    // are part of the signature — otherwise the hover keeps the old name
    // until the next lap happens to redraw the chart.
    + ':' + laps.map((l) => l.driver ?? '').join('|') : '';

  useEffect(() => {
    if (!laps.length || !eRef.current || !tRef.current) return;
    // ONE BAR PER LAP, LABELLED WITH THE CAR'S OWN LAP NUMBER.
    //
    // x is the lap's POSITION in finish order, never its number: lap numbers
    // repeat and can go backwards (the pit corrects the count with set_lap, or
    // a checkpoint is wiped), and a repeated x stacks two laps on one bar.
    // The numbers come back as tick TEXT, so the axis still reads 11, 12, 13.
    //
    // This used to fall back to 1..N whenever the numbers were not strictly
    // increasing -- which silently renumbered the whole race the moment one
    // correction landed, while the exported workbook's Lap column kept the
    // car's numbers. The axis and the spreadsheet then disagreed about which
    // lap was which, with nothing on screen to say so. Positions with real
    // labels never drift from the workbook, whatever the numbers do.
    const base = layoutBase(dark, 230);
    const t = theme(dark);
    const lapX = laps.map((_, i) => i);
    // HOVER IS THE LAP NUMBER AND THE VALUE, AND NOTHING ELSE.
    //
    // x is a position, not a lap number (see above), so the number has to be
    // carried in customdata or the hover names the wrong lap. Everything else
    // a lap has -- driver, kind, time stood, flags -- is a column in the Laps
    // by driver table below, where it can be read and compared instead of
    // chased with a mouse.
    const lapNo = laps.map((l) => l.lap);
    // One label per lap (dtick: 1) made Plotly measure hundreds of labels: 358
    // laps took 4.5-5.4 s to draw, against 54 ms with a coarser step. Handing
    // it ~12 explicit ticks keeps that win: tickmode 'array' is what lets the
    // label say the lap number while the position stays the coordinate.
    const step = Math.max(1, Math.ceil(lapX.length / 12));
    const lapTicks: number[] = [];
    for (let i = 0; i < laps.length; i += step) lapTicks.push(i);
    const lapAxis = {
      title: { text: 'lap', font: { size: 11, color: t.ink3 } },
      tickmode: 'array' as const,
      tickvals: lapTicks,
      ticktext: lapTicks.map((i) => String(laps[i].lap)),
    };
    void Plotly.newPlot(eRef.current, [{
      type: 'bar', x: lapX, y: laps.map((l) => l.energyWh),
      marker: { color: laps.map((l) => (counts(l.kind) ? '#00B3FF' : MUTED)),
                line: { width: 0 } }, width: 0.55,
      customdata: lapNo, hovertemplate: 'lap %{customdata} · %{y:.1f} Wh<extra></extra>',
    }], { ...base, margin: { l: 50, r: 12, t: 8, b: 36 }, bargap: 0.4,
          xaxis: { ...base.xaxis, ...lapAxis },
          // DRAG PANS, as on History, Weather and the battery forecast.
          // layoutBase defaults to zoom; on a chart with one bar per lap a
          // drag is nearly always "show me the laps either side of these",
          // and a race puts hundreds of them off the end of the axis. The
          // modebar still has zoom and reset for the times it is not.
          dragmode: 'pan',
          showlegend: false }, plotConfig);
    // Lap time is plotted in SECONDS and labelled m:ss, never decimal minutes.
    // "4.45 min" is not a figure anyone on a pit wall thinks in; 4:27 is the
    // same number in the form the lap tiles, the header clock and the driver's
    // own stopwatch all use. Plotly has no duration axis, so the ticks are
    // placed by hand rather than left to it.
    const secs = laps.map((l) => l.lapTimeS)
                     .filter((v): v is number => v !== null && Number.isFinite(v));
    const lo = secs.length ? Math.min(...secs) : 0;
    const hi = secs.length ? Math.max(...secs) : 0;
    // A step that lands on round seconds and leaves at most ~8 labels, so the
    // axis stays legible whether the laps differ by 3 s or by 3 minutes.
    const tStep = [1, 2, 5, 10, 15, 20, 30, 60, 120, 300, 600]
      .find((s) => (hi - lo) / s <= 8) ?? 900;
    const tickvals: number[] = [];
    for (let v = Math.floor(lo / tStep) * tStep;
         v <= Math.ceil(hi / tStep) * tStep + tStep / 2; v += tStep) tickvals.push(v);
    // One lap, or every lap to the second identical: a lone tick reads like a
    // broken axis, so give it a second one to sit against.
    if (tickvals.length < 2) tickvals.push(tickvals[0] + tStep);
    void Plotly.newPlot(tRef.current, [{
      type: 'scatter', mode: 'lines+markers', x: lapX,
      y: laps.map((l) => l.lapTimeS),
      connectgaps: false, line: { color: '#00e0b4', width: 2 },
      marker: { size: 8, color: laps.map((l) => (counts(l.kind) ? '#00e0b4' : MUTED)),
                line: { width: 2, color: t.card } },
      customdata: laps.map((l) => [l.lap, lapTimeShort(l.lapTimeS)]),
      hovertemplate: 'lap %{customdata[0]} · %{customdata[1]}<extra></extra>',
    }], { ...base, margin: { l: 50, r: 12, t: 8, b: 36 },
          xaxis: { ...base.xaxis, ...lapAxis },
          yaxis: { ...base.yaxis, tickmode: 'array', tickvals,
                   ticktext: tickvals.map((v) => lapTimeShort(v)) },
          dragmode: 'pan',
          showlegend: false }, plotConfig);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sig, dark]);

  return (
    <Disclosure icon="timer" title="Per-lap energy & times" count={laps.length} open>
      {!laps.length
        ? <Pill kind="info">No completed laps yet. Laps appear once the car crosses the finish line (or the pit cuts one manually).</Pill>
        : (
          <>
            <div className="grid g2">
              <div><div className="caption">Energy per lap (Wh)</div><div ref={eRef} /></div>
              <div><div className="caption">Lap time (m:ss)</div><div ref={tRef} /></div>
            </div>
            <div className="kv" style={{ gridTemplateColumns: 'repeat(4, 1fr)', marginTop: 10 }}>
              <div><div className="k">Laps recorded</div><div className="v">{summary?.count ?? laps.length}
                {someNotFlying &&
                  <span className="caption"> · {summary?.flyingCount} flying</span>}</div></div>
              <div><div className="k">Best lap</div><div className="v mono">{lapTime(summary?.bestS ?? null)}</div></div>
              <div><div className="k">Average lap</div><div className="v mono">{lapTime(summary?.avgS ?? null)}</div></div>
              <div><div className="k">Average Wh / lap</div><div className="v mono">{fmtStat(summary?.avgWh ?? null)}</div></div>
            </div>
            {someNotFlying && (
              <div className="caption" style={{ marginTop: 6 }}>
                Best and averages use flying laps only. In-laps, out-laps and laps the car
                marked suspect are drawn grey — the <b>Kind</b> column below says which.
              </div>
            )}
            <LapTable laps={laps} drivers={drivers} onEdited={() => setEdits((n) => n + 1)} />
          </>
        )}
    </Disclosure>
  );
}

/** Every lap with the name of whoever drove it, newest first.
 *
 *  THE DRIVER COMES FROM THE PIT, NOT THE CAR. Nothing on the CAN bus knows who
 *  is in the seat, so a lap's driver is whichever stint the pit had logged when
 *  that lap finished. A lap driven before anyone pressed "Driver changed", or
 *  by a crew that never typed a name, therefore shows — and that is the honest
 *  answer: it is a lap nobody recorded a name for, not a lap with no driver.
 *
 *  Newest first, because during a race the laps being read are the last few.
 *  The workbook's Laps sheet runs the other way (oldest first, as the race was
 *  driven); the columns are deliberately the same ones in the same order, so a
 *  row here and a row there are read as the same record.
 */
/** WHO DROVE A LAP CAN BE SET BY HAND, per lap or for a run of laps.
 *
 *  The stint log answers first, and it is only as good as the presses made
 *  during a pit stop: on 2026-09-19 one press in the wrong order credited
 *  eighteen of Ido's laps to Amit. The dropdown is the pit's word over the log.
 *  It is filed per lap on the server and read back by the same function the
 *  Excel workbooks use, so a fix made here is the name in the export too. The
 *  small arrow on an edited lap drops the edit and goes back to the log.
 *
 *  Names come from the team's list (config.drivers), never typed, because
 *  "ido" and "Ido" are two drivers in a pivot table. */
function LapTable({ laps, drivers, onEdited }: {
  laps: LapsResp['laps']; drivers: string[]; onEdited: () => void;
}) {
  const rows = [...laps].reverse();
  const named = laps.some((l) => l.driver);
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [who, setWho] = useState('');
  const [busy, setBusy] = useState(false);

  const setDriver = async (keys: string[], driver: string, what: string) => {
    setBusy(true);
    try {
      await postJSON('/api/laps/driver', { keys, driver });
      toast(driver ? `${what} → ${driver}` : `${what} → back to the stint log`);
      onEdited();
    } catch (e) { toast(`Driver not changed: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  const a = Number(from), b = Number(to);
  const range = from.trim() !== '' && to.trim() !== '' && Number.isInteger(a) && Number.isInteger(b) && a <= b
    ? laps.filter((l) => l.lap >= a && l.lap <= b) : [];
  return (
    <Disclosure icon="table" title="Laps by driver" count={laps.length}>
      {!named && (
        <Pill kind="info">
          No driver logged for these laps. Names come from the driver stint on the
          sidebar — press “Name current driver”, and every lap from then on is credited.
        </Pill>
      )}
      {/* A RUN OF LAPS AT ONCE. A stint credited to the wrong name is a dozen
          laps or more, and nobody should fix that one dropdown at a time. */}
      <div className="btnrow" style={{ margin: '8px 0', alignItems: 'center' }}>
        <span className="caption" style={{ margin: 0 }}>Set laps</span>
        <input type="text" inputMode="numeric" placeholder="from" aria-label="From lap" value={from}
               onChange={(e) => setFrom(e.target.value)} style={{ width: 64 }} disabled={busy} />
        <span className="caption" style={{ margin: 0 }}>to</span>
        <input type="text" inputMode="numeric" placeholder="to" aria-label="To lap" value={to}
               onChange={(e) => setTo(e.target.value)} style={{ width: 64 }} disabled={busy} />
        <select className="rangeselect" value={who} onChange={(e) => setWho(e.target.value)} disabled={busy} aria-label="Driver">
          <option value="">driver…</option>
          {drivers.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
        <button className="btn" disabled={busy || !who || !range.length}
                onClick={() => setDriver(range.map((l) => l.key), who, `${range.length} laps (${a}–${b})`)
                  .then(() => { setFrom(''); setTo(''); setWho(''); })}>
          Apply{range.length ? ` to ${range.length}` : ''}
        </button>
      </div>
      <div className="scroll" style={{ marginTop: named ? 0 : 8 }}>
        <table className="tbl">
          <thead>
            <tr>
              <th className="num">Lap</th><th>Driver</th>
              <th className="num">Started</th>
              <th className="num">Lap time</th><th className="num">Energy (Wh)</th>
              <th className="num">Distance (m)</th><th>Kind</th>
              <th className="num">Stood (s)</th><th>Flags</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((l, i) => (
              <tr key={`${l.lap}-${i}`} className={counts(l.kind) ? undefined : 'not-flying'}>
                <td className="num mono">{l.lap}</td>
                <td>
                  <select className="cellselect" value={l.driver ?? ''} disabled={busy}
                          aria-label={`Driver of lap ${l.lap}`}
                          onChange={(e) => setDriver([l.key], e.target.value, `Lap ${l.lap}`)}>
                    <option value="">{MISSING}</option>
                    {/* A name from the stint log that is not on the list still
                        has to show as itself, not as a dash. */}
                    {l.driver && !drivers.includes(l.driver) && <option value={l.driver}>{l.driver}</option>}
                    {drivers.map((d) => <option key={d} value={d}>{d}</option>)}
                  </select>
                  {l.driverEdited && (
                    <button className="linkbtn" title="Set by hand. Click to go back to the stint log."
                            disabled={busy} onClick={() => setDriver([l.key], '', `Lap ${l.lap}`)}>↺</button>
                  )}
                </td>
                <td className="num mono">{l.started ?? MISSING}</td>
                <td className="num mono">{lapTime(l.lapTimeS)}</td>
                <td className="num">{fmtStat(l.energyWh)}</td>
                <td className="num">{fmtStat(l.distanceM)}</td>
                <td>{l.kind ? (KIND_NAME[l.kind] ?? l.kind) : MISSING}</td>
                <td className="num">{l.stoppedS === null ? MISSING : Math.round(l.stoppedS)}</td>
                <td>{l.flags?.length ? l.flags.join(', ') : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Disclosure>
  );
}

function RecentSamples({ config, visible }: { config: Config; visible: boolean }) {
  const { data } = usePoll(() => getJSON<{ rows: Record<string, Num | string>[] }>('/api/samples?limit=60'), 10000, [], visible);
  return (
    <Disclosure icon="table" title="Recent samples" count={data?.rows?.length ?? 0}>
      <div className="scroll">
        <table className="tbl">
          <thead>
            <tr><th>Time</th>{config.metrics.map((m) => <th key={m.key} className="num">{m.label}</th>)}</tr>
          </thead>
          <tbody>
            {(data?.rows ?? []).map((r, i) => (
              <tr key={i}>
                <td className="mono">{String(r.t ?? MISSING).replace('T', ' ').slice(5, 19)}</td>
                {config.metrics.map((m) => <td key={m.key} className="num">{fmtStat(r[m.key] as Num)}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Disclosure>
  );
}

function Faults({ visible }: { visible: boolean }) {
  const { data } = usePoll(() => getJSON<{ episodes: { sig: string; start: string; durationS: number; samples: number }[] }>('/api/faults'), 30000, [], visible);
  const eps = data?.episodes ?? [];
  return (
    <Disclosure icon="alert" title="Fault history" count={eps.length}>
      {!eps.length
        ? <Pill kind="ok">No faults recorded in history.</Pill>
        : (
          <div className="scroll">
            <table className="tbl">
              <thead><tr><th>Start</th><th className="num">Duration</th><th className="num">Samples</th><th>Fault</th></tr></thead>
              <tbody>
                {eps.map((e, i) => (
                  <tr key={i}>
                    <td className="mono">{e.start.replace('T', ' ').slice(0, 19)}</td>
                    <td className="num">{e.durationS.toFixed(1)}s</td>
                    <td className="num">{e.samples}</td>
                    <td>{e.sig}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </Disclosure>
  );
}
