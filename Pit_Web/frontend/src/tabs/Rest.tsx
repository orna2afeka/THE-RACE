// Live Metrics, Weather and Strategy.

import { useEffect, useMemo, useRef, useState } from 'react';
import * as Plotly from 'plotly.js-dist-min';
import { CarControls, CatalogueTile, MetricTile, Pill, SectionTitle } from '../components';
import { Icon } from '../icons';
import { MISSING, ageText, fmt, getJSON, postJSON, usePoll, useResizePlot, useStored } from '../lib';
import { config as plotConfig, layoutBase, theme } from '../plotly-theme';
import { toast } from '../toast';
import type { CellExtremesResp, CellTileData, CellsResp, Config, Live, MatrixResp, MatrixRow, StrategyResp } from '../types';

/* ------------------------------- Live Metrics ---------------------------- */
const GROUP_ICON: Record<string, string> = {
  'Motion': 'gauge', 'Motor': 'zap', 'Driver Input': 'gauge',
  'Battery': 'battery', 'Energy': 'bolt', 'Lap & Distance': 'flag',
};

/** The one-pedal control, as a bar the pit reads the same way the driver does.
 *
 *  The pedal commands REGEN below its neutral point and POWER above it (see
 *  efficiency.py), so the scale is raw millivolts with neutral marked, and the
 *  fill grows OUT FROM NEUTRAL — left and blue into regen, right and red into
 *  acceleration. Filling from the left end instead would light the whole regen
 *  half whenever the driver was accelerating hard, which is the one thing a
 *  one-pedal control can never be doing.
 *
 *  Identical geometry and colours to the driver's DS001 bar
 *  (driver_dash_v2.PedalBar), because the pit coaches against what the driver
 *  is looking at. Every number here comes from the API: the browser holds no
 *  copy of the neutral point.
 *
 *  A pedal that is not reporting draws an empty track and a dash — never a bar
 *  at zero, which would read as a driver sitting at neutral. */
const PEDAL_REGEN = '#2e86de';
const PEDAL_ACCEL = '#d63447';

function PedalBar({ live, config }: { live: Live; config: Config }) {
  // Optional because the API and this page are separate processes: a page
  // newer than its backend gets a config with no `pedal` in it, and
  // destructuring that threw where the tile should simply not appear.
  if (!config.pedal) return null;
  const { idleMv, neutralMv, fullMv } = config.pedal;
  const mv = live.state.throttle_mv;
  const accel = live.state.throttle_pct;
  const regen = live.state.regen_pct;

  const pct = (v: number) => Math.max(0, Math.min(100, (v / fullMv) * 100));
  const neutralPct = pct(neutralMv);
  const pedalPct = mv === null ? null : pct(mv);

  // Which command is being given, and how much of it. Non-zero regen wins:
  // at most one of the two is ever non-zero, and inside the deadband either
  // side of neutral both are 0 — which is coasting, not "0 % of" either.
  const label = mv === null ? MISSING
    : regen ? `REGEN ${fmt(regen, '.0f')}%`
    : accel ? `ACCEL ${fmt(accel, '.0f')}%`
    : 'COAST';
  const labelColour = mv === null ? undefined : regen ? PEDAL_REGEN : accel ? PEDAL_ACCEL : undefined;

  return (
    <div className="tile large pedal-tile">
      <div className="tile-label">Pedal</div>
      <div className="pedal-row">
        <div className="pedal-value" style={{ color: labelColour }}>{label}</div>
        <div className="pedal-track">
          <div className="pedal-zone" style={{ left: 0, width: `${neutralPct}%`, background: PEDAL_REGEN }} />
          <div className="pedal-zone" style={{ left: `${neutralPct}%`, right: 0, background: PEDAL_ACCEL }} />
          {pedalPct !== null && (
            <div
              className="pedal-fill"
              style={pedalPct < neutralPct
                ? { left: `${pedalPct}%`, width: `${neutralPct - pedalPct}%`, background: PEDAL_REGEN }
                : { left: `${neutralPct}%`, width: `${pedalPct - neutralPct}%`, background: PEDAL_ACCEL }}
            />
          )}
          <div className="pedal-neutral" style={{ left: `${neutralPct}%` }} />
        </div>
      </div>
      <div className="tile-note">
        {`regen below ${fmt(neutralMv, '.0f')} mV, acceleration above it · released pedal is `}
        {`${fmt(idleMv, '.0f')} mV (full regen), floored is ${fmt(fullMv, '.0f')} mV`}
        {mv === null ? '' : ` · now ${fmt(mv, '.0f')} mV`}
      </div>
    </div>
  );
}

export function LiveMetrics({ live, config }: { live: Live; config: Config }) {
  return (
    <>
      {!live.fresh && (
        <Pill kind="warn">
          Not live — every reading below is from the last sample received, {ageText(live.age)} ago.
        </Pill>
      )}
      {live.liveMetrics.map((g) => (
        <div key={g.group}>
          <SectionTitle icon={GROUP_ICON[g.group] ?? 'activity'} title={g.group} right={`${g.metrics.length} tiles`} />
          <div className="grid g4">
            {g.metrics.map((m) => <CatalogueTile key={m.label} m={m} />)}
          </div>
          {/* Under its own group's tiles: the bar is the same three numbers
              (throttle %, zone, raw mV) drawn as one picture, and reading it
              beside them is what makes the millivolts mean something. */}
          {g.group === 'Driver Input' && <PedalBar live={live} config={config} />}
          {g.group === 'Lap & Distance' && <TripReset carLink={!config.demoStore} />}
        </div>
      ))}
      <div className="caption" style={{ marginTop: 16 }}>
        {config.liveMetricCount} metrics · refreshing every 2 s · — means the car has not reported
        that field · thresholds and colours come from limits.py, shared with the driver HUD.
      </div>
    </>
  );
}

/* --------------------------------- Weather ------------------------------- */
interface WeatherResp {
  available: boolean;
  rows: {
    t: string; temp: number | null; cloud: number | null; radiation: number | null;
    rain: number | null; rainChance: number | null;
  }[];
}

const RAIN_BLUE = '#3498db';

export function Weather({ dark }: { dark: boolean }) {
  const { data } = usePoll(() => getJSON<WeatherResp>('/api/weather'), 600000);
  const sunRef = useRef<HTMLDivElement | null>(null);
  const rainRef = useRef<HTMLDivElement | null>(null);
  useResizePlot(sunRef);
  useResizePlot(rainRef);

  // Drag pans by default, as on History and Strategy. One setting for both charts.
  const [dragmode, setDragmode] = useStored<'pan' | 'zoom'>('pit.weatherDrag', 'pan',
    (v) => v === 'pan' || v === 'zoom');
  const dragRef = useRef(dragmode);
  dragRef.current = dragmode;
  useEffect(() => {
    for (const el of [sunRef.current, rainRef.current]) {
      const g = el as unknown as { data?: unknown[] } | null;
      if (el && g?.data) void Plotly.relayout(el, { dragmode });
    }
  }, [dragmode]);

  useEffect(() => {
    if (!data?.available || !sunRef.current || !rainRef.current) return;
    const base = layoutBase(dark, 300);
    const t = theme(dark);
    const x = data.rows.map((r) => r.t);
    const axisTitle = (text: string) => ({ text, font: { size: 11, color: t.ink3 } });

    void Plotly.newPlot(sunRef.current, [{
      type: 'scatter', mode: 'lines', fill: 'tozeroy',
      x, y: data.rows.map((r) => r.radiation),
      line: { color: '#f1c40f', width: 2 }, fillcolor: 'rgba(241,196,15,0.10)',
      hovertemplate: '%{y:.0f} W/m²<extra></extra>',
    }], {
      ...base, margin: { l: 56, r: 16, t: 10, b: 40 }, showlegend: false,
      yaxis: { ...base.yaxis, title: axisTitle('W/m²'), rangemode: 'tozero' },
      dragmode: dragRef.current,
    }, plotConfig);

    // Amount as bars on the left axis, chance as a line on a fixed 0-100 right axis.
    // A missing hour stays null: a gap in the chart, never a zero.
    void Plotly.newPlot(rainRef.current, [{
      type: 'bar', name: 'Rain', x, y: data.rows.map((r) => r.rain),
      marker: { color: RAIN_BLUE, opacity: 0.8 },
      hovertemplate: '%{y:.1f} mm<extra></extra>',
    }, {
      type: 'scatter', mode: 'lines', name: 'Chance', x, y: data.rows.map((r) => r.rainChance), yaxis: 'y2',
      line: { color: t.ink3, width: 2, dash: 'dot' },
      hovertemplate: '%{y:.0f} %<extra></extra>',
    }], {
      ...base, margin: { l: 56, r: 48, t: 10, b: 40 },
      showlegend: true, legend: { orientation: 'h', x: 0, y: 1.02, yanchor: 'bottom', font: { color: t.ink3 } },
      // Never auto-scale a dry day up to fill the chart: 1 mm/h stays the minimum top.
      yaxis: { ...base.yaxis, title: axisTitle('mm'), rangemode: 'tozero',
               range: [0, Math.max(1, ...data.rows.map((r) => r.rain ?? 0)) * 1.1] },
      yaxis2: { overlaying: 'y', side: 'right', range: [0, 100], showgrid: false, zeroline: false,
                title: axisTitle('%'), tickfont: { color: t.ink3 } },
      dragmode: dragRef.current,
    }, plotConfig);
  }, [data, dark]);

  if (!data) return <div className="caption">loading forecast…</div>;
  if (!data.available) {
    return <Pill kind="warn">
      Weather API unavailable — this is the one panel that needs the internet, and the pit LAN is usually offline by design.
    </Pill>;
  }
  const dragChips = (
    <div className="toolbar">
      <div className="chips">
        <span className="lbl">Drag</span>
        <button className="chip" aria-pressed={dragmode === 'pan'} onClick={() => setDragmode('pan')}>Pan</button>
        <button className="chip" aria-pressed={dragmode === 'zoom'} onClick={() => setDragmode('zoom')}>Zoom</button>
      </div>
    </div>
  );
  const time = (t: string) => t.replace('T', ' ').slice(5, 16);
  return (
    <>
      <SectionTitle icon="sun" title="Solar irradiance forecast" right="next 24 h · Open-Meteo · cached 1 h" />
      {dragChips}
      <div className="split">
        <div className="chart"><div ref={sunRef} /></div>
        <div className="scroll" style={{ maxHeight: 320 }}>
          <table className="tbl">
            <thead><tr><th>Time</th><th className="num">Cloud %</th><th className="num">°C</th><th className="num">W/m²</th></tr></thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.t}>
                  <td className="mono">{time(r.t)}</td>
                  <td className="num">{fmt(r.cloud)}</td>
                  <td className="num">{fmt(r.temp, '.1f')}</td>
                  <td className="num">{fmt(r.radiation)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <SectionTitle icon="cloud" title="Rain forecast" right="next 24 h · Open-Meteo · cached 1 h" />
      {dragChips}
      <div className="split">
        <div className="chart"><div ref={rainRef} /></div>
        <div className="scroll" style={{ maxHeight: 320 }}>
          <table className="tbl">
            <thead><tr><th>Time</th><th className="num">Chance %</th><th className="num">mm</th></tr></thead>
            <tbody>
              {data.rows.map((r) => (
                <tr key={r.t}>
                  <td className="mono">{time(r.t)}</td>
                  <td className="num">{fmt(r.rainChance)}</td>
                  <td className="num">{fmt(r.rain, '.1f')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

/* -------------------------------- Strategy ------------------------------- */
// ONE SIMULATION FEEDS BOTH THE TABLE AND THE CHART. The server plans each
// strategy once and sends the rows and the (minute, Wh) traces together in one
// payload. The chart below DRAWS those traces. It derives nothing: not a lap,
// not a charge, not even the floor line. If this component ever recomputes
// anything, it has rebuilt the bug where the table was computed from a
// tapering charge curve while the chart drew straight lines from a flat rate.

const STRATEGY_COLOURS = ['#e74c3c', '#e67e22', '#f1c40f', '#3498db', '#9b59b6'];

/** Minutes the way the crew says them out loud: "3h 34m", "47m". */
const hm = (min: number) => {
  const m = Math.max(0, Math.round(min));
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m` : `${m}m`;
};

// Everything the chart draws. Two polls with the same signature are the same
// picture, so an unchanged plan does not redraw at all.
const chartSig = (d: StrategyResp) => JSON.stringify([d.traces, d.floorWh, d.capacityWh]);

function BatteryChart({ data, dark }: { data: StrategyResp; dark: boolean }) {
  const ref = useRef<HTMLDivElement | null>(null);
  useResizePlot(ref);

  // Drag pans by default, as on History. Remembered per browser; switched
  // with relayout, so the view is not lost.
  const [dragmode, setDragmode] = useStored<'pan' | 'zoom'>('pit.stratDrag', 'pan',
    (v) => v === 'pan' || v === 'zoom');
  const dragRef = useRef(dragmode);
  dragRef.current = dragmode;
  useEffect(() => {
    const g = ref.current as unknown as { data?: unknown[] } | null;
    if (g && g.data) void Plotly.relayout(ref.current as HTMLDivElement, { dragmode });
  }, [dragmode]);

  // HOLD STILL WHILE BEING READ. The poll lands every 10 s; a chart that
  // redraws under the pointer loses the hover label and jumps mid-pan. So the
  // chart keeps its own snapshot and only takes the new plan when nobody is
  // looking: pointer outside, no button held, view not panned or zoomed.
  // A double-click (Plotly's reset) or "Update chart" lets it go again.
  const [hovering, setHovering] = useState(false);
  const [pressed, setPressed] = useState(false);
  const [viewMoved, setViewMoved] = useState(false);
  const [shown, setShown] = useState(data);
  const held = hovering || pressed || viewMoved;
  useEffect(() => { if (!held) setShown(data); }, [data, held]);
  useEffect(() => {
    if (!pressed) return;
    const up = () => setPressed(false);
    window.addEventListener('pointerup', up);
    return () => window.removeEventListener('pointerup', up);
  }, [pressed]);
  const shownSig = useMemo(() => chartSig(shown), [shown]);
  const behind = useMemo(() => chartSig(data), [data]) !== shownSig;
  const listening = useRef(false);

  useEffect(() => {
    if (!ref.current) return;
    const data = shown;
    const base = layoutBase(dark, 340);
    const t = theme(dark);
    const traces: Parameters<typeof Plotly.newPlot>[1] = [];
    // Every plan is simulated over the SAME window -- the engine gives each one
    // the time that is left -- so one clock serves them all.
    const maxT = data.traces.reduce((m, tr) => Math.max(m, tr?.totalTimeMin ?? 0), 0);

    // WHEN, not only how much. The hover box is unified, and Plotly builds its
    // title from the x value and nothing else: no number format turns 213.6
    // into "3h 34m left, 1h 26m in", and none can reach a second number. So the
    // clock rides in as a trace of its own -- no line, out of the legend, one
    // point per minute of the window, carrying both readings as its hover text.
    // It draws nothing and is first in the list, so it is the first row of the
    // box. It is also why the title is blanked in the layout below: with the
    // clock row there, the raw minute count above it is noise.
    const clock = Array.from({ length: Math.floor(maxT) + 1 }, (_, m) => m);
    traces.push({
      type: 'scatter', mode: 'lines', name: '', showlegend: false,
      x: clock, y: clock.map(() => 0),
      line: { color: 'rgba(0,0,0,0)', width: 0 },
      // x is minutes REMAINING, so the elapsed side is the rest of the window.
      text: clock.map((m) => `${hm(m)} left · ${hm(maxT - m)} in`),
      hovertemplate: '%{text}<extra></extra>',
    });

    data.traces.forEach((tr, i) => {
      if (!tr) return;
      const c = STRATEGY_COLOURS[i % STRATEGY_COLOURS.length];
      // Minutes REMAINING on x, so the race runs left to right and the flag
      // is at x = 0. Every point is the engine's own; charge segments are
      // sampled along the real curve, so they are genuinely concave.
      traces.push({
        type: 'scatter', mode: 'lines', name: tr.label,
        x: tr.points.map((p) => Math.max(0, tr.totalTimeMin - p.minute)),
        y: tr.points.map((p) => p.wh),
        line: { color: c, width: 2 },
        hovertemplate: '%{y:.0f} Wh<extra>' + tr.label + '</extra>',
      });
      if (tr.stops.length) {
        traces.push({
          type: 'scatter', mode: 'markers', name: `${tr.label} stops`, showlegend: false,
          x: tr.stops.map((s) => Math.max(0, tr.totalTimeMin - s.atMin)),
          y: tr.stops.map((s) => tr.capacityWh * s.socBefore / 100),
          text: tr.stops.map((s) => `stop ${s.number} after lap ${s.afterLap} → ${s.socAfter.toFixed(0)}% · ${s.stopMin.toFixed(0)} min`),
          marker: { symbol: 'triangle-down', size: 9, color: c, line: { color: dark ? '#fff' : '#000', width: 0.6 } },
          hovertemplate: '%{text}<extra>' + tr.label + '</extra>',
        });
      }
    });

    // Plotly.react redraws IN PLACE, keeping the viewer's zoom across the
    // 10 s poll. It is core Plotly and present in the bundle; the dist-min
    // type file just does not declare it, so it is reached through newPlot's
    // identical signature.
    const react = (Plotly as unknown as { react: typeof Plotly.newPlot }).react;
    void react(ref.current, traces, {
      ...base, margin: { l: 56, r: 16, t: 28, b: 42 },
      xaxis: {
        ...base.xaxis, autorange: 'reversed',
        title: { text: 'minutes remaining', font: { size: 11, color: t.ink3 } },
        // Blank, because the clock trace above already opens the box with the
        // time in words. A single space, not '': Plotly reads an empty string
        // as "unset" and puts the raw x value back.
        unifiedhovertitle: { text: ' ' },
      },
      yaxis: { ...base.yaxis, range: [0, data.capacityWh * 1.05], title: { text: 'Wh', font: { size: 11, color: t.ink3 } } },
      shapes: [{
        type: 'line', xref: 'x', yref: 'y', x0: 0, x1: Math.max(maxT, 1), y0: data.floorWh, y1: data.floorWh,
        line: { color: '#e74c3c', width: 1.5, dash: 'dash' },
      }],
      annotations: [{
        x: Math.max(maxT, 1), y: data.floorWh, xref: 'x', yref: 'y', text: `${data.floorWh.toFixed(0)} Wh floor`,
        showarrow: false, xanchor: 'left', yanchor: 'bottom', font: { size: 10, color: '#e74c3c' },
      }],
      dragmode: dragRef.current,
      // Keeps the viewer's pan/zoom when "Update chart" redraws under it.
      uirevision: 'strategy',
    }, plotConfig).then((gd) => {
      if (listening.current) return;
      listening.current = true;
      gd.on('plotly_relayout', (e) => {
        const keys = Object.keys((e ?? {}) as Record<string, unknown>);
        if (keys.some((k) => k.endsWith('autorange'))) setViewMoved(false);
        else if (keys.some((k) => k.includes('range'))) setViewMoved(true);
      });
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shownSig, dark]);

  return (
    <>
      <div className="toolbar">
        <div className="chips">
          <span className="lbl">Drag</span>
          <button className="chip" aria-pressed={dragmode === 'pan'} onClick={() => setDragmode('pan')}>Pan</button>
          <button className="chip" aria-pressed={dragmode === 'zoom'} onClick={() => setDragmode('zoom')}>Zoom</button>
        </div>
      </div>
      <div className="chart"
           onPointerEnter={() => setHovering(true)}
           onPointerLeave={() => setHovering(false)}
           onPointerDown={() => setPressed(true)}>
        <div ref={ref} />
      </div>
      {/* Below the chart, never above: appearing above would shove the chart
          down under the pointer, the exact jump this hold exists to prevent. */}
      {(viewMoved || behind) && (
        <div className="hist-badge frozen" style={{ marginTop: 8 }}>
          <span className="dot" />
          <span>{behind
            ? 'HOLDING — a newer plan is in the table above; the chart waits until you stop looking'
            : 'HOLDING — view moved, the chart will not refresh · double-click it to reset'}</span>
          <span style={{ marginLeft: 'auto' }} className="btnrow">
            <button className="btn" onClick={() => { setViewMoved(false); setShown(data); }}>
              <Icon name="play" size={12} />Update chart
            </button>
          </span>
        </div>
      )}
    </>
  );
}

/* ----------------------------- Matrix editor ----------------------------- */
// The matrix is the plan, so this is the one place on the dashboard that
// CHANGES the plan. Three rules it is built around:
//
//   1. Nothing is written until Apply. Typing, and filling from a row, move a
//      draft the server has never seen.
//   2. What Apply will change is listed in words first. A matrix edited at
//      3 a.m. with the car on track is not the moment to discover that the
//      anchor was the wrong row.
//   3. The arithmetic is the SERVER's (energy_model.ladder_from_anchor). The
//      browser sends the row somebody typed and draws what comes back — there
//      is no second copy of the energy model in here to drift from the one the
//      strategy engine plans with.

/** "4:45" or "285" -> 285. Returns null for anything that is not a lap time. */
function parseLap(text: string): number | null {
  const t = text.trim();
  if (!t) return null;
  const parts = t.split(':');
  if (parts.length > 2) return null;
  const nums = parts.map((p) => Number(p));
  if (nums.some((n) => !Number.isFinite(n) || n < 0)) return null;
  const secs = parts.length === 2 ? nums[0] * 60 + nums[1] : nums[0];
  return secs > 0 ? secs : null;
}

function lapText(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds - m * 60;
  return m + ':' + (s < 10 ? '0' : '') + s.toFixed(s % 1 ? 1 : 0);
}

function MatrixEditor({ onSaved, onCancel }:
  { onSaved: () => void; onCancel: () => void }) {
  const [saved, setSaved] = useState<MatrixRow[] | null>(null);
  const [draft, setDraft] = useState<Record<string, { lap: string; wh: string }>>({});
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getJSON<MatrixResp>('/api/strategy/matrix').then((m) => {
      setSaved(m.rows);
      setDraft(Object.fromEntries(m.rows.map((r) =>
        [r.key, { lap: lapText(r.target_s), wh: String(r.energy_wh) }])));
    }).catch((e) => toast('Matrix unavailable: ' + e, 'err'));
  }, []);

  const set = (key: string, field: 'lap' | 'wh', value: string) =>
    setDraft((d) => ({ ...d, [key]: { ...d[key], [field]: value } }));

  const parsed = (r: MatrixRow) => ({
    lap: parseLap(draft[r.key]?.lap ?? ''),
    wh: Number(draft[r.key]?.wh),
  });
  const rowBad = (r: MatrixRow) => {
    const p = parsed(r);
    return p.lap == null || !Number.isFinite(p.wh) || p.wh <= 0;
  };
  const anyBad = (saved ?? []).some(rowBad);

  // What Apply will write, in words. Same idea as the Profile Builder's diff:
  // the crew reads the change, not the resulting table.
  const changes = (saved ?? []).flatMap((r) => {
    const p = parsed(r);
    const out: string[] = [];
    if (p.lap != null && Math.abs(p.lap - r.target_s) > 0.05)
      out.push(r.label + ' lap ' + lapText(r.target_s) + ' → ' + lapText(p.lap));
    if (Number.isFinite(p.wh) && Math.abs(p.wh - r.energy_wh) > 0.05)
      out.push(r.label + ' ' + r.energy_wh.toFixed(1) + ' → ' + p.wh.toFixed(1) + ' Wh');
    return out;
  });

  const fill = async (r: MatrixRow) => {
    const p = parsed(r);
    if (p.lap == null || !Number.isFinite(p.wh) || p.wh <= 0) {
      toast('Give this row a lap time and a Wh first', 'err');
      return;
    }
    setBusy(true);
    try {
      const out = await postJSON<{ rows: MatrixRow[] }>(
        '/api/strategy/matrix/fill', { key: r.key, target_s: p.lap, energy_wh: p.wh });
      setDraft(Object.fromEntries(out.rows.map((x) =>
        [x.key, { lap: lapText(x.target_s), wh: String(x.energy_wh) }])));
      toast('Filled the other rows from ' + r.label);
    } catch (e) { toast('Fill failed: ' + e, 'err'); }
    setBusy(false);
  };

  const apply = async () => {
    if (!saved || anyBad) return;
    setBusy(true);
    try {
      await postJSON('/api/strategy/matrix', {
        rows: saved.map((r) => {
          const p = parsed(r);
          return { key: r.key, target_s: p.lap, energy_wh: p.wh };
        }),
      });
      toast('Matrix updated — ' + changes.length + ' change(s), no restart needed');
      onSaved();
    } catch (e) { toast('Not saved: ' + e, 'err'); }
    setBusy(false);
  };

  if (!saved) return <div className="card pad"><span className="muted">Loading the matrix…</span></div>;
  return (
    <div className="card pad">
      <table className="tbl">
        <thead><tr>
          <th>Profile</th><th className="num">Lap time</th><th className="num">Wh / lap</th><th></th>
        </tr></thead>
        <tbody>
          {saved.map((r) => (
            <tr key={r.key}>
              <td>{r.label}</td>
              <td className="num">
                <input value={draft[r.key]?.lap ?? ''} size={7} inputMode="decimal"
                       onChange={(e) => set(r.key, 'lap', e.target.value)}
                       style={{ width: 80, textAlign: 'right',
                                borderColor: parsed(r).lap == null ? 'var(--pit-warning)' : undefined }} />
              </td>
              <td className="num">
                <input value={draft[r.key]?.wh ?? ''} size={7} inputMode="decimal"
                       onChange={(e) => set(r.key, 'wh', e.target.value)}
                       style={{ width: 80, textAlign: 'right',
                                borderColor: !(Number(draft[r.key]?.wh) > 0) ? 'var(--pit-warning)' : undefined }} />
              </td>
              <td>
                <button className="btn" disabled={busy} onClick={() => fill(r)}
                        title="Keep this row and rebuild the other four around it: lap times at the spacing the matrix already has, Wh from the rolling+drag model anchored here.">
                  <Icon name="target" size={12} />Fill from this row
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="caption" style={{ marginTop: 8 }}>
        Lap time takes <code>4:45</code> or <code>285</code>. <b>Fill from this row</b> keeps the row you
        typed and derives the rest: lap times keep the spacing this matrix already has, and Wh comes from
        rolling + drag (<code>E = a·L + b·L³/t²</code>), not a flat percentage — slowing down saves less
        than a percentage ladder claims, because rolling loss is paid per metre. One row cannot separate
        the two terms, so drag is taken as ⅓ of that lap; two measured paces remove the assumption
        (<code>energy_model.py --from-db</code>).
      </div>
      {changes.length > 0 && (
        <div className="caption" style={{ marginTop: 6 }}>
          <b>Apply will write:</b> {changes.join(' · ')}.
        </div>
      )}
      <div className="btnrow" style={{ marginTop: 10 }}>
        <button className="btn primary" onClick={apply} disabled={busy || anyBad || !changes.length}>
          <Icon name="check" size={13} />Apply{changes.length ? ' (' + changes.length + ')' : ''}
        </button>
        <button className="btn" onClick={onCancel} disabled={busy}>Cancel</button>
        <span className="caption">
          {anyBad ? 'Every row needs a lap time and a Wh.'
            : 'Writes Pit_Dashboard/constants.py (old copy kept in profiles/_backup/) and takes effect at the next poll. The car is not touched — it keeps flying the speed profile behind each key.'}
        </span>
      </div>
    </div>
  );
}

/* --------------------------- Strategy inputs ----------------------------- */
// DEMO DASHBOARD ONLY. The search takes three numbers — how much charge is in
// the pack, how much of the race is left, and which lap the car is on — and on
// race day all three come from the car and the race clock. When the Pi has
// been silent for hours the stored SoC is whatever it was before it went
// quiet, so the plan on the screen is a plan for a car that no longer exists.
//
// This panel lets those three be typed instead, on a dashboard that is not
// reading the real store. The server decides whether to honour them (see
// DEMO_STORE in api.py) and echoes back what it used, so this cannot label a
// car-derived plan as typed.

/** "14:00" or "840" -> 840 minutes. Null for anything that is not a duration. */
function parseMinutes(text: string): number | null {
  const t = text.trim();
  if (!t) return null;
  const parts = t.split(':');
  if (parts.length > 2) return null;
  const nums = parts.map((x) => Number(x));
  if (nums.some((n) => !Number.isFinite(n) || n < 0)) return null;
  const mins = parts.length === 2 ? nums[0] * 60 + nums[1] : nums[0];
  return mins >= 0 ? mins : null;
}

function minutesText(mins: number): string {
  const m = Math.round(mins);
  const h = Math.floor(m / 60);
  return h ? `${h} h ${String(m % 60).padStart(2, '0')} m` : `${m} m`;
}

export interface StrategyOverride { socPct: number | null; timeLeftMin: number | null }

function StrategyInputs({ applied, onApply, onClear, manualLap, setManualLap, data }: {
  applied: StrategyOverride;
  onApply: (o: StrategyOverride) => void;
  onClear: () => void;
  manualLap: number;
  setManualLap: (n: number) => void;
  data: StrategyResp | null;
}) {
  const [soc, setSoc] = useState(applied.socPct == null ? '' : String(applied.socPct));
  const [left, setLeft] = useState(applied.timeLeftMin == null ? '' : String(applied.timeLeftMin));
  const [lap, setLap] = useState(manualLap >= 0 ? String(manualLap) : '');

  const socNum = soc.trim() === '' ? null : Number(soc);
  // Above 0, not from 0: the strategy engine reads an empty pack as an unknown
  // one and plans a full one, so "0" here would silently become 100%.
  const socBad = socNum !== null && (!Number.isFinite(socNum) || socNum <= 0 || socNum > 100);
  const leftNum = parseMinutes(left);
  // The server caps this at the race duration and serves the cap, so the
  // number is not typed in here. More time than the race lasts is not a
  // question the plan can answer.
  const leftBad = left.trim() !== ''
    && (leftNum === null || (data != null && leftNum > data.maxTimeLeftMin));
  const lapNum = lap.trim() === '' ? null : Number(lap);
  const lapBad = lapNum !== null && (!Number.isInteger(lapNum) || lapNum < 0);
  const anyBad = socBad || leftBad || lapBad;

  const apply = () => {
    if (anyBad) return;
    setManualLap(lapNum === null ? -1 : lapNum);
    onApply({ socPct: socNum, timeLeftMin: leftNum });
  };
  const clear = () => {
    setSoc(''); setLeft(''); setLap('');
    setManualLap(-1);
    onClear();
  };

  const bad = { borderColor: 'var(--pit-warning)' };
  return (
    <div className="card pad">
      <table className="tbl">
        <thead><tr>
          <th>Input</th><th className="num">Plan with</th><th>Leave empty and it uses</th>
        </tr></thead>
        <tbody>
          <tr>
            <td>Battery SoC</td>
            <td className="num">
              <input value={soc} size={7} inputMode="decimal" placeholder="—"
                     onChange={(e) => setSoc(e.target.value)}
                     style={{ width: 80, textAlign: 'right', ...(socBad ? bad : {}) }} /> %
            </td>
            <td className="caption" style={{ margin: 0 }}>
              {data?.carSocPct == null
                ? 'nothing — the car has never reported one, so a full pack is assumed'
                : <>the car's last reading, <b>{fmt(data.carSocPct, '.1f')}%</b></>}
            </td>
          </tr>
          <tr>
            <td>Time remaining</td>
            <td className="num">
              <input value={left} size={7} inputMode="text" placeholder="—"
                     onChange={(e) => setLeft(e.target.value)}
                     style={{ width: 80, textAlign: 'right', ...(leftBad ? bad : {}) }} />
            </td>
            <td className="caption" style={{ margin: 0 }}>
              the race clock, <b>{data ? minutesText(data.clockTimeLeftMin) : '—'}</b>
            </td>
          </tr>
          <tr>
            <td>Lap</td>
            <td className="num">
              <input value={lap} size={7} inputMode="numeric" placeholder="—"
                     onChange={(e) => setLap(e.target.value)}
                     style={{ width: 80, textAlign: 'right', ...(lapBad ? bad : {}) }} />
            </td>
            <td className="caption" style={{ margin: 0 }}>
              the car's lap count. This is the same override as the sidebar's
              <b> Manual lap</b>, not a second one.
            </td>
          </tr>
        </tbody>
      </table>
      <div className="caption" style={{ marginTop: 8 }}>
        Time remaining takes <code>14:00</code> or <code>840</code>, both meaning 840 minutes left.
        Nothing here is written anywhere: these are the arguments the plan is searched with, and
        clearing them puts it straight back on the car's numbers. The matrix above is untouched —
        edit that to change what a lap <i>costs</i>; edit this to change what the car <i>has</i>.
      </div>
      <div className="btnrow" style={{ marginTop: 10 }}>
        <button className="btn primary" onClick={apply} disabled={anyBad}>
          <Icon name="check" size={13} />Plan with these
        </button>
        <button className="btn" onClick={clear}>Back to the car's values</button>
        <span className="caption">
          {anyBad ? `SoC is above 0 and up to 100, time is minutes or h:mm up to ${minutesText(data?.maxTimeLeftMin ?? 0)}, lap is a whole number.`
            : 'Demo dashboard only — the real pit dashboard always plans from the car.'}
        </span>
      </div>
    </div>
  );
}

export function Strategy({ config, manualLap, setManualLap, dark, selected }:
  { config: Config; manualLap: number; setManualLap: (n: number) => void; dark: boolean;
    /** The profile the pit has already chosen, from the live feed, or
     *  undefined while nobody has chosen and the target is assumed. */
    selected?: string }) {
  // Bumped when the matrix is edited, so the plan is re-fetched at once
  // instead of on the next 10 s tick — the edit is the one moment the crew is
  // watching for the table to move.
  const [matrixVersion, setMatrixVersion] = useState(0);
  const [editing, setEditing] = useState(false);
  // Demo-only typed inputs. Kept across a reload, because the reason to type
  // them — the car is not reporting — outlives a page refresh. The server
  // ignores them on the real store whatever is in here.
  const [editingInputs, setEditingInputs] = useState(false);
  const [override, setOverride] = useStored<StrategyOverride>(
    'pit.strategyInputs', { socPct: null, timeLeftMin: null },
    (v) => !!v && typeof v === 'object');
  const ovQuery = (override.socPct == null ? '' : `&soc_pct=${override.socPct}`)
    + (override.timeLeftMin == null ? '' : `&time_left_min=${override.timeLeftMin}`);
  const { data } = usePoll(() => getJSON<StrategyResp>(`/api/strategy?manual_lap=${manualLap}${ovQuery}`), 10000, [manualLap, matrixVersion, ovQuery]);
  const [choice, setChoice] = useState(selected ?? config.defaultStrategyKey);
  // Adopt the stored selection ONCE, when the live feed first carries one. Not
  // on every poll: this tab can be open with the dropdown half-changed, and
  // resetting it under the strategist's hand two seconds before they press Send
  // is how the wrong profile gets sent.
  const adopted = useRef(selected != null);
  useEffect(() => {
    if (!adopted.current && selected) { adopted.current = true; setChoice(selected); }
  }, [selected]);
  const [sent, setSent] = useState<string | null>(null);
  const [sentId, setSentId] = useState<number | null>(null);
  const { data: ack } = usePoll(
    () => getJSON<{ ack: { strategy?: string; applied?: boolean; id?: number } | null }>('/api/strategy/ack'), 5000, [sent ?? '']);
  // Retained node: without the id this said "Car confirmed it is running X"
  // from an ack the car wrote in a previous session.
  const confirmed = !!ack?.ack?.applied && ack.ack.id != null && ack.ack.id === sentId;

  const send = async () => {
    try {
      const r = await postJSON<{ id: number }>('/api/strategy/select', { key: choice });
      setSentId(r.id);
      setSent(new Date().toLocaleTimeString());
      toast(`Strategy sent: ${profiles.find((s) => s.key === choice)?.label ?? choice}`);
    } catch (e) { toast(`Send failed: ${e}`, 'err'); }
  };

  // The selector follows the matrix, not the page-load config: edit a lap
  // time and the name beside it stops being true within one poll.
  const profiles = (data?.matrix ?? config.strategies.map((s) => ({
    key: s.key, label: s.label, target_s: s.lap_time_min * 60, energy_wh: s.energy_wh,
  }))).map((s) => ({ key: s.key, label: s.label, lap: lapText(s.target_s) }));

  // From the payload, so it is true only once the server has actually planned
  // with the typed values. Gated on demoStore as well, which is what keeps the
  // real dashboard exactly as it was: there a manual lap is the only override
  // there has ever been, and it has never raised a banner.
  const overridden = !!data && data.demoStore
    && (data.overrides.socPct != null || data.overrides.timeLeftMin != null
        || manualLap >= 0);
  const cols = data?.rows.length ? Object.keys(data.rows[0]) : [];
  const isNum = (v: unknown) => typeof v === 'number';
  const measuredLabels = data ? Object.keys(data.measured) : [];
  return (
    <>
      <SectionTitle icon="route" title="Active strategy" />
      <div className="card pad">
        {/* The profile dropdown is fine to play with anywhere -- it is a local
            choice until Send. SEND is the car command, so the whole row is
            switched off where there is no car to reach. */}
        <CarControls enabled={!config.demoStore}
                     note="Pick a profile and read the plan all you like; sending it to the car needs the real pit dashboard.">
        <div className="btnrow">
          <select value={choice} onChange={(e) => setChoice(e.target.value)} style={{ maxWidth: 300 }}>
            {profiles.map((s) => <option key={s.key} value={s.key}>{s.label} · {s.lap}</option>)}
          </select>
          <button className="btn primary" onClick={send}><Icon name="send" size={13} />Send to car</button>
        </div>
        </CarControls>
        {/* The ack line is the ONLY place the car's own answer is shown. Every
            target readout on this dashboard follows the selection above, so if
            the car disagrees, this caption is where the crew sees it. */}
        <div className="caption">
          {sent
            ? (confirmed
                ? `Car confirmed it is running ${ack?.ack?.strategy ?? '(profile not named)'} · sent ${sent}`
                : `Sent ${sent} — awaiting the car's confirmation. This changes the driver's target speed and corner warnings.`)
            : `Only the profile name is sent; the car holds all five profiles. Sending also sets the pit's own target speed${selected ? ` — now ${selected}` : ', which is assumed until you send one'}.`}
        </div>
      </div>

      <SectionTitle icon="table" title="Strategy matrix"
                    right={data ? `${data.timeLeftMin.toFixed(0)} min remaining · max ${data.maxStops} charges (regulation) · each ${data.minStopMin.toFixed(0)}–${data.maxStopMin.toFixed(0)} min` : undefined} />
      <div className="btnrow" style={{ marginBottom: 8 }}>
        <button className="btn" onClick={() => setEditing((v) => !v)}>
          <Icon name="sliders" size={13} />{editing ? 'Close editor' : 'Edit matrix'}
        </button>
        {/* Only where the server will honour it. config.demoStore and the
            payload's demoStore are the same flag; this uses config so the
            button is there before the first plan arrives. */}
        {config.demoStore && (
          <button className="btn" aria-pressed={overridden} onClick={() => setEditingInputs((v) => !v)}>
            <Icon name="target" size={13} />{editingInputs ? 'Close inputs' : 'Edit inputs'}
          </button>
        )}
        {!editing && !editingInputs && <span className="caption">Lap time and Wh per lap, edited here and live at the next poll — no restart.</span>}
      </div>
      {editing && (
        <MatrixEditor
          onSaved={() => { setEditing(false); setMatrixVersion((v) => v + 1); }}
          onCancel={() => setEditing(false)} />
      )}
      {editingInputs && config.demoStore && (
        <StrategyInputs applied={override} data={data}
                        manualLap={manualLap} setManualLap={setManualLap}
                        onApply={(o) => { setOverride(o); setEditingInputs(false); }}
                        onClear={() => { setOverride({ socPct: null, timeLeftMin: null }); setEditingInputs(false); }} />
      )}
      {/* Says what the SERVER used, not what this page asked for. Stays up
          while the panel is closed: a plan made from typed numbers must never
          be readable as the car's. */}
      {overridden && (
        <Pill kind="warn">
          <b>Planning from typed inputs, not from the car.</b>{' '}
          {[data!.overrides.socPct != null
              ? `SoC ${fmt(data!.overrides.socPct, '.1f')}% (car: ${data!.carSocPct == null ? 'never reported' : fmt(data!.carSocPct, '.1f') + '%'})`
              : null,
            data!.overrides.timeLeftMin != null
              ? `${minutesText(data!.overrides.timeLeftMin)} remaining (race clock: ${minutesText(data!.clockTimeLeftMin)})`
              : null,
            manualLap >= 0 ? `lap ${manualLap}` : null,
          ].filter(Boolean).join(' · ')}.
        </Pill>
      )}
      {data?.assumedFullPack && (
        <Pill kind="warn">
          Assuming a full pack / lap 0{data.missing.length ? ` — no ${data.missing.join(' or ')} from the car yet` : ''}.
          These figures are a placeholder until it reports.
        </Pill>
      )}
      <div className="scroll" style={{ maxHeight: 'none' }}>
        <table className="tbl">
          <thead><tr>{cols.map((c) => <th key={c} className={isNum(data?.rows[0]?.[c]) ? 'num' : ''}>{c}</th>)}</tr></thead>
          <tbody>
            {(data?.rows ?? []).map((r, i) => (
              <tr key={i}>
                {cols.map((c) => (
                  <td key={c} className={isNum(r[c]) ? 'num' : ''}
                      /* Stop by stop, from the same trace the chart draws: what
                         the charge does, and what the box actually costs once
                         the floor is paid. The column stays short; the detail
                         is here. */
                      title={c === 'Charge Time' && data?.traces[i]?.stops.length
                        ? data.traces[i]!.stops.map((s) =>
                            `stop ${s.number} (after lap ${s.afterLap}): `
                            + `${s.socBefore.toFixed(0)}% → ${s.socAfter.toFixed(0)}% `
                            + `in ${s.chargeMin.toFixed(0)} min, ${s.stopMin.toFixed(0)} min in the box`)
                            .join('\n')
                        /* The sum, spelled out. Pit Time is stationary time,
                           so it is larger than the charging in Charge Time
                           beside it, and the difference is the changes. */
                        : c === 'Pit Time' && data?.traces[i]
                        ? `${data.traces[i]!.pitMin.toFixed(0)} min stationary = `
                          + `${data.traces[i]!.chargeStopMin.toFixed(0)} min at ${data.traces[i]!.stops.length} charge `
                          + `stop${data.traces[i]!.stops.length === 1 ? '' : 's'} + `
                          + `${data.traces[i]!.swapMin.toFixed(0)} min of ${data.traces[i]!.swaps} mid-stint driver `
                          + `change${data.traces[i]!.swaps === 1 ? '' : 's'}. `
                          + `A change made at a charge stop is free — the car is stopped anyway.`
                        : undefined}>
                    {String(r[c])}
                    {c === 'Energy/Lap (Wh)' && data?.measured[String(r.Label)] != null
                      ? <span className={Math.abs(data.measured[String(r.Label)].wh - data.measured[String(r.Label)].storedWh)
                                         > 0.1 * data.measured[String(r.Label)].storedWh ? 'limit-tag' : 'measured-tag'}
                              title={`the matrix plans ${data.measured[String(r.Label)].storedWh.toFixed(1)} Wh here; `
                                   + `${data.measured[String(r.Label)].laps} laps the car drove on this profile cost `
                                   + `${data.measured[String(r.Label)].wh.toFixed(1)} Wh (median). The table shows the matrix.`}>
                          car: {data.measured[String(r.Label)].wh.toFixed(0)}
                        </span>
                      : null}
                    {c === 'Pit Strategy' && String(r[c]).includes('limit')
                      ? <span className="limit-tag" title="All 3 charges the regulations allow were used and the car then sat idle long enough that another would have fitted. A 4th charge is not an option: it ranks the car behind every car that charged 3 times.">stop-limited</span>
                      : null}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {/* Provenance, not a second opinion. Which rows the car actually paid
          for, and whether the charge times are real: a measured plan and an
          estimated one justify very different confidence in "three more
          stints". */}
      {data && (
        <div className="caption" style={{ marginTop: 8 }}>
          <>Energy per lap is <b>the matrix as the crew set it</b> — it changes when someone changes it, not when a stint is logged.{' '}
            {measuredLabels.length
              ? <>What the car actually paid: {measuredLabels.map((l) => `${l} ${data.measured[l].wh.toFixed(1)} Wh over ${data.measured[l].laps} laps (matrix: ${data.measured[l].storedWh.toFixed(1)})`).join('; ')}.{' '}
                  {measuredLabels.some((l) => Math.abs(data.measured[l].wh - data.measured[l].storedWh) > 0.1 * data.measured[l].storedWh)
                    ? <span className="warn-text">That is more than 10% off the plan — worth re-costing the matrix.</span>
                    : <span className="ok-text">Within 10% of the plan.</span>}</>
              : <>No profile has {data.minLapsForMeasured} completed laps yet, so there is nothing to compare it against.</>}</>
        </div>
      )}
      {data && (
        <div className="caption">
          {data.chargingCurveIsMeasured
            ? <><span className="ok-text">Charge times from the measured pack curve.</span> <b>Charge Time</b> is one entry per stop, in the order of <b>Charge To</b>; a stop costs at least {data.minStopMin.toFixed(0)} min however quick the charge, and the charge stops at {data.maxStopMin.toFixed(0)} min wherever the SoC has got to. <b>Pit Time</b> is every stationary minute — the charge stops plus the mid-stint driver changes in <b>Driver Swaps</b>; a change made at a stop is free. Hover it for the sum.</>
            : <><span className="warn-text">Charge times are MODELLED, not measured.</span> The SoC curve came from the charging branch marked <i>example data</i> and has never been checked against this charger or pack — its shape is right, its numbers are not ours. Lap counts are sound; treat <b>Pit Time</b> as an estimate until someone times a real charge.</>}
        </div>
      )}

      <SectionTitle icon="activity" title="Battery forecast"
                    right="the simulation the table came from · ▼ = pit entry · holds still while you look" />
      {data
        ? <BatteryChart data={data} dark={dark} />
        : <div className="caption">loading…</div>}
    </>
  );
}

/* ------------------------------ Cell Voltages ---------------------------- */
// DS003 per-cell temperature and DS004 per-module voltage, the compliance
// screen. Everything is classified on the server against the same Threshold
// the driver HUD uses; this only lays it out.

function CellTile({ c, spec, unit }: { c: CellTileData; spec: string; unit: string }) {
  return <MetricTile title={c.label} value={fmt(c.value, spec)} unit={unit} tier={c.tier} />;
}

/* Rule 3.5.6: highest / lowest cell temperature and voltage over the last
   2 hours. Everything with a time or a threshold in it arrives built and
   classified; this only lays it out. The server caches the report for
   refreshS seconds, so polling faster than that costs nothing. */
function CellExtremes() {
  const { data } = usePoll(() => getJSON<CellExtremesResp>('/api/cell_extremes'), 10000);
  const title = <SectionTitle icon="flag" title="Rule 3.5.6 — Cell Extremes, Last 2 Hours" />;
  if (!data) return <>{title}<div className="caption">loading report…</div></>;
  if (data.state === 'empty') return <>{title}<Pill kind="info">No telemetry stored yet.</Pill></>;
  return (
    <>
      {title}
      <div className="caption">
        {data.state === 'none' && <>Window ending {data.end} (newest sample): no plausible cell reading in it.</>}
        {data.state === 'full' && <>Window <b>{data.span}</b> · full 2 h of data · ends at the newest stored sample · refreshed every {data.refreshS} s</>}
        {data.state === 'partial' && <><span className="amber">Window <b>{data.span}</b> · only {data.coverText} of cell data in the last 2 h</span> · refreshed every {data.refreshS} s</>}
      </div>
      <div className="grid g4 extremes">
        {data.tiles.map((t) => (
          <MetricTile key={t.key} title={t.title} value={fmt(t.value, t.spec)} unit={t.unit} tier={t.tier} note={t.note} />
        ))}
      </div>
    </>
  );
}

function ProbeTile({ c }: { c: CellsResp['probes'][number]['cells'][number] }) {
  return <MetricTile title={c.label} value={fmt(c.value, '.1f')} unit="°C" tier={c.tier}
                     stale={c.stale} note={c.stale ? `· ${ageText(c.staleS)} ago` : null} />;
}

export function Cells() {
  const { data } = usePoll(() => getJSON<CellsResp>('/api/cells'), 2000);
  if (!data) return <><CellExtremes /><div className="caption">loading cells…</div></>;
  const { temps, voltages } = data;
  return (
    <>
      {!data.fresh && (
        <Pill kind="warn">Not live — every reading below is from the last sample received, {ageText(data.age)} ago.</Pill>
      )}

      <CellExtremes />

      <SectionTitle icon="thermo" title="BMS Probe Temperatures (live)" />
      {data.probes.map((p) => (
        <div key={p.pack} className="grid g3 probes">
          {p.cells.map((c) => <ProbeTile key={c.id} c={c} />)}
        </div>
      ))}

      <SectionTitle icon="thermometer" title="DS003 — Cell temperatures" right={`warn above ${temps.warn.toFixed(0)} °C · critical above ${temps.crit.toFixed(0)} °C`} />
      {!temps.configured ? (
        <Pill kind="info">
          <b>Not configured yet.</b> The Orion Thermistor Expansion Module has not had any sensors loaded or enabled via
          its own Thermistor Utility software, so no per-cell temperature has ever been reported. This switches to real
          readings automatically, with no dashboard change, the first time the car sends one.
        </Pill>
      ) : (
        <>
          {temps.groups.map((g) => (
            <div key={g.name}>
              <div className="caption" style={{ marginTop: 6 }}><b>Module {g.name}</b> ({g.label} · sensor ids {g.lo}-{g.hi})</div>
              <div className="cellgrid">{g.cells.map((c) => <CellTile key={c.id} c={c} spec=".1f" unit="°C" />)}</div>
            </div>
          ))}
          {temps.unmapped.length > 0 && (
            <div>
              <div className="caption" style={{ marginTop: 6 }}><b>Unmapped sensors</b> (ids {temps.unmapped.map((c) => c.id).join(', ')}) — reporting from outside the pack's two 13-cell modules, shown so no reading is lost.</div>
              <div className="cellgrid">{temps.unmapped.map((c) => <CellTile key={c.id} c={c} spec=".1f" unit="°C" />)}</div>
            </div>
          )}
        </>
      )}

      <SectionTitle icon="battery" title={`DS004 — Module voltages (1–${voltages.required})`}
                    right={voltages.stringCount != null ? `BMS reports ${voltages.stringCount} cells wired` : 'cell count unknown — bms_string_count not reported yet'} />
      {voltages.ok
        ? <Pill kind="ok"><b>DS004 OK — {voltages.valid}/{voltages.required} module voltages live.</b></Pill>
        : <Pill kind="err">
            <b>DS004 NOT MET — {voltages.valid}/{voltages.required} module voltages live.</b> Missing module(s): {voltages.missing}.
            The pit polls every cell frame; these are not being answered. That is a BMS configuration or cell-tap wiring
            matter on the car, not a dashboard one — check the pack's configured series count and the sense harness.
          </Pill>}
      <div className="caption">A monitored cell IS a module here: the JBD BMS protocol has no grouping above individual cells. Colour: warn below {voltages.warn} V, critical below {voltages.crit} V.</div>
      <div className="cellgrid">{voltages.modules.map((c) => <CellTile key={c.id} c={c} spec=".3f" unit="V" />)}</div>
      {voltages.extra.length > 0 && (
        <>
          <SectionTitle icon="battery" title={`Additional cells (${voltages.extraRange})`} right="wired beyond the rulebook's 26 · shown so no data is lost" />
          <div className="cellgrid">{voltages.extra.map((c) => <CellTile key={c.id} c={c} spec=".3f" unit="V" />)}</div>
        </>
      )}
    </>
  );
}

/* -------------------------------- Trip reset ----------------------------- */
// Sits under the Trip tile, where it was asked for. Asks the CAR to zero its
// own tracked distance total. Not lap count, not energy, and not the
// controller's hardware TRIP register, for which there is no CAN command.

export function TripReset({ carLink }: { carLink: boolean }) {
  const [sent, setSent] = useState<string | null>(null);
  const [sentId, setSentId] = useState<number | null>(null);
  const { data: ack } = usePoll(
    () => getJSON<{ ack: { applied?: boolean; action?: string; id?: number } | null }>('/api/trip_reset/ack'), 5000, [sent ?? '']);
  // The ack node is shared with the lap commands AND retained, so this used to
  // read a Cut Lap ack from hours earlier as "trip reset confirmed". Match the
  // id the send returned, and the action.
  const confirmed = !!ack?.ack?.applied && ack.ack.action === 'reset_trip'
    && ack.ack.id != null && ack.ack.id === sentId;
  return (
    <div className="card pad" style={{ marginTop: 8 }}>
      <CarControls enabled={carLink}>
      <div className="btnrow">
        <button className="btn" onClick={async () => {
          try {
            const r = await postJSON<{ sentAt: string; id: number }>('/api/trip_reset', {});
            setSentId(r.id);
            setSent(r.sentAt);
            toast('Trip reset sent to the car');
          } catch (e) { toast(`Trip reset failed: ${e}`, 'err'); }
        }}><Icon name="history" size={13} />Reset trip</button>
        <span className="caption" style={{ margin: 0 }}>
          {sent
            ? (confirmed ? `Car confirmed — trip reset · sent ${sent}` : `Sent ${sent} — awaiting the car's confirmation.`)
            : "Zeroes the car's Trip distance. Does not touch the controller's odometer, lap count or energy."}
        </span>
      </div>
      </CarControls>
    </div>
  );
}
