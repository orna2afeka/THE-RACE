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

import { useEffect, useRef, useState } from 'react';
import * as Plotly from 'plotly.js-dist-min';
import { Disclosure, Pill, SectionTitle } from '../components';
import { Icon } from '../icons';
import { MISSING, fmtStat, getJSON, lapTime, usePoll, useResizePlot } from '../lib';
import { config as plotConfig, layoutBase, theme } from '../plotly-theme';
import type { Config, HistoryResponse, Num, StatRow } from '../types';

interface Props { config: Config; dark: boolean; visible: boolean }

interface AppendMsg {
  type: 'append';
  t: (string | null)[];
  series: Record<string, Num[]>;
  cursor: number;
}

/** Past this many points on the SVG chart, reload the window (which re-thins
 *  it evenly). At 1 Hz that is ~8 hours of appends on a 15-minute window. */
const POINT_CEILING = 30000;

export default function History({ config, dark, visible }: Props) {
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

  const minutes = config.historyWindows[windowLabel];
  const chosen = config.metrics.filter((m) => selected.includes(m.key));
  const qs = `metrics=${encodeURIComponent(selected.join(','))}` + (minutes ? `&minutes=${minutes}` : '');

  const { data: stats } = usePoll(
    () => getJSON<{ stats: StatRow[] }>(`/api/history/stats?${qs}`), 10000, [qs]);

  // Purge only on true unmount — never between reloads, so the old chart stays
  // visible while the next window loads.
  useEffect(() => () => { if (holder.current) Plotly.purge(holder.current); }, []);

  // ---- newPlot per (metrics, window), then appends forever ---------------- //
  useEffect(() => {
    if (!selected.length) return;
    let cancelled = false;
    let ws: WebSocket | null = null;
    const host = holder.current;
    setAppended(0);
    setLoading(true);

    // Remember the user's zoom if this redraw is for the SAME window.
    const sameWindow = lastWindowRef.current === windowLabel;
    lastWindowRef.current = windowLabel;
    const prevAxis = (host as Plotly.PlotlyDiv | null)?.layout?.xaxis;
    const keepRange = sameWindow && prevAxis?.autorange === false && Array.isArray(prevAxis.range)
      ? prevAxis.range : null;

    (async () => {
      const hist = await getJSON<HistoryResponse>(`/api/history?${qs}`);
      if (cancelled || !host) return;
      pointsRef.current = hist.count;
      setEmpty(hist.count === 0);

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

      const base = layoutBase(dark, 470);
      await Plotly.newPlot(host, traces, {
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
        annotations,
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
        if (cancelled || !host || pausedRef.current) return;
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
  }, [qs, normalize, dark, reloadKey]);

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

      <div className={`hist-badge ${paused ? 'frozen' : 'live'}`}>
        <span className="dot" />
        <span>{paused ? 'PAUSED — new samples buffered, chart holding still' : 'LIVE — appending every 10 s · zoom and pan survive'}</span>
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
          <button className="btn" onClick={() => setPaused((p) => !p)}>
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

      <LapCharts dark={dark} visible={visible} />
      <RecentSamples config={config} />
      <Faults />
    </>
  );
}

// --------------------------------------------------------------------------- //
interface LapsResp {
  laps: { lap: number; energyWh: Num; lapTimeS: Num; distanceM: Num }[];
  summary: { count: number; bestS: Num; avgS: Num; avgWh: Num };
}

function LapCharts({ dark, visible }: { dark: boolean; visible: boolean }) {
  const { data } = usePoll(() => getJSON<LapsResp>('/api/laps'), 10000);
  const eRef = useRef<HTMLDivElement | null>(null);
  const tRef = useRef<HTMLDivElement | null>(null);
  useResizePlot(eRef, visible);
  useResizePlot(tRef, visible);

  useEffect(() => {
    if (!data?.laps.length || !eRef.current || !tRef.current) return;
    const laps = data.laps.map((l) => l.lap);
    const base = layoutBase(dark, 230);
    const t = theme(dark);
    void Plotly.newPlot(eRef.current, [{
      type: 'bar', x: laps, y: data.laps.map((l) => l.energyWh),
      marker: { color: '#00B3FF', line: { width: 0 } }, width: 0.55,
      hovertemplate: '%{y:.1f} Wh<extra>lap %{x}</extra>',
    }], { ...base, margin: { l: 50, r: 12, t: 8, b: 36 }, bargap: 0.4,
          xaxis: { ...base.xaxis, title: { text: 'lap', font: { size: 11, color: t.ink3 } }, dtick: 1 },
          showlegend: false }, plotConfig);
    void Plotly.newPlot(tRef.current, [{
      type: 'scatter', mode: 'lines+markers', x: laps,
      y: data.laps.map((l) => (l.lapTimeS === null ? null : l.lapTimeS / 60)),
      connectgaps: false, line: { color: '#00e0b4', width: 2 },
      marker: { size: 8, color: '#00e0b4', line: { width: 2, color: t.card } },
      hovertemplate: '%{y:.2f} min<extra>lap %{x}</extra>',
    }], { ...base, margin: { l: 50, r: 12, t: 8, b: 36 },
          xaxis: { ...base.xaxis, title: { text: 'lap', font: { size: 11, color: t.ink3 } }, dtick: 1 },
          showlegend: false }, plotConfig);
  }, [data, dark]);

  return (
    <Disclosure icon="timer" title="Per-lap energy & times" count={data?.laps.length ?? 0} open>
      {!data?.laps.length
        ? <Pill kind="info">No completed laps yet. Laps appear once the car crosses the finish line (or the pit cuts one manually).</Pill>
        : (
          <>
            <div className="grid g2">
              <div><div className="caption">Energy per lap (Wh)</div><div ref={eRef} /></div>
              <div><div className="caption">Lap time (minutes)</div><div ref={tRef} /></div>
            </div>
            <div className="kv" style={{ gridTemplateColumns: 'repeat(4, 1fr)', marginTop: 10 }}>
              <div><div className="k">Laps recorded</div><div className="v">{data.summary.count}</div></div>
              <div><div className="k">Best lap</div><div className="v mono">{lapTime(data.summary.bestS)}</div></div>
              <div><div className="k">Average lap</div><div className="v mono">{lapTime(data.summary.avgS)}</div></div>
              <div><div className="k">Average Wh / lap</div><div className="v mono">{fmtStat(data.summary.avgWh)}</div></div>
            </div>
          </>
        )}
    </Disclosure>
  );
}

function RecentSamples({ config }: { config: Config }) {
  const { data } = usePoll(() => getJSON<{ rows: Record<string, Num | string>[] }>('/api/samples?limit=60'), 10000);
  return (
    <Disclosure icon="table" title="Recent samples" count={data?.rows.length ?? 0}>
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

function Faults() {
  const { data } = usePoll(() => getJSON<{ episodes: { sig: string; start: string; durationS: number; samples: number }[] }>('/api/faults'), 30000);
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
