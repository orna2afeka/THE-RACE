// Live Metrics, Weather and Strategy.

import { useEffect, useMemo, useRef, useState } from 'react';
import * as Plotly from 'plotly.js-dist-min';
import { CatalogueTile, MetricTile, Pill, SectionTitle } from '../components';
import { Icon } from '../icons';
import { ageText, fmt, getJSON, postJSON, usePoll, useResizePlot, useStored } from '../lib';
import { config as plotConfig, layoutBase, theme } from '../plotly-theme';
import { toast } from '../toast';
import type { CellExtremesResp, CellTileData, CellsResp, Config, Live, StrategyResp } from '../types';

/* ------------------------------- Live Metrics ---------------------------- */
const GROUP_ICON: Record<string, string> = {
  'Motion': 'gauge', 'Motor': 'zap',
  'Battery': 'battery', 'Energy': 'bolt', 'Lap & Distance': 'flag',
};

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
          {g.group === 'Lap & Distance' && <TripReset />}
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
    let maxT = 0;
    data.traces.forEach((tr, i) => {
      if (!tr) return;
      const c = STRATEGY_COLOURS[i % STRATEGY_COLOURS.length];
      maxT = Math.max(maxT, tr.totalTimeMin);
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
      xaxis: { ...base.xaxis, autorange: 'reversed', title: { text: 'minutes remaining', font: { size: 11, color: t.ink3 } } },
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

export function Strategy({ config, manualLap, dark, selected }:
  { config: Config; manualLap: number; dark: boolean;
    /** The profile the pit has already chosen, from the live feed, or
     *  undefined while nobody has chosen and the target is assumed. */
    selected?: string }) {
  const { data } = usePoll(() => getJSON<StrategyResp>(`/api/strategy?manual_lap=${manualLap}`), 10000, [manualLap]);
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
  const { data: ack } = usePoll(
    () => getJSON<{ ack: { strategy?: string; applied?: boolean } | null }>('/api/strategy/ack'), 5000, [sent ?? '']);

  const send = async () => {
    try {
      await postJSON('/api/strategy/select', { key: choice });
      setSent(new Date().toLocaleTimeString());
      toast(`Strategy sent: ${config.strategies.find((s) => s.key === choice)?.label ?? choice}`);
    } catch (e) { toast(`Send failed: ${e}`, 'err'); }
  };

  const cols = data?.rows.length ? Object.keys(data.rows[0]) : [];
  const isNum = (v: unknown) => typeof v === 'number';
  const measuredLabels = data ? Object.keys(data.measured) : [];
  return (
    <>
      <SectionTitle icon="route" title="Active strategy" />
      <div className="card pad">
        <div className="btnrow">
          <select value={choice} onChange={(e) => setChoice(e.target.value)} style={{ maxWidth: 300 }}>
            {config.strategies.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
          <button className="btn primary" onClick={send}><Icon name="send" size={13} />Send to car</button>
        </div>
        {/* The ack line is the ONLY place the car's own answer is shown. Every
            target readout on this dashboard follows the selection above, so if
            the car disagrees, this caption is where the crew sees it. */}
        <div className="caption">
          {sent
            ? (ack?.ack?.applied
                ? `Car confirmed it is running ${ack.ack.strategy ?? '(profile not named)'} · sent ${sent}`
                : `Sent ${sent} — awaiting the car's confirmation. This changes the driver's target speed and corner warnings.`)
            : `Only the profile name is sent; the car holds all five profiles. Sending also sets the pit's own target speed${selected ? ` — now ${selected}` : ', which is assumed until you send one'}.`}
        </div>
      </div>

      <SectionTitle icon="table" title="Strategy matrix"
                    right={data ? `${data.timeLeftMin.toFixed(0)} min remaining · max ${data.maxStops} charges (regulation) · each at least ${data.minStopMin.toFixed(0)} min` : undefined} />
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
                  <td key={c} className={isNum(r[c]) ? 'num' : ''}>
                    {String(r[c])}
                    {c === 'Energy/Lap (Wh)' && data?.measured[String(r.Label)] != null
                      ? <span className="measured-tag" title={`median of ${data.measured[String(r.Label)]} laps the car drove on this profile`}>measured</span>
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
          {measuredLabels.length
            ? <><span className="ok-text">Energy per lap measured from the car</span> for {measuredLabels.map((l) => `${l} (${data.measured[l]} laps)`).join(', ')}. The rest are estimates.</>
            : <><span className="warn-text">Energy per lap is estimated</span> — no profile has {data.minLapsForMeasured} completed laps yet. These become measurements once one does.</>}
        </div>
      )}
      {data && (
        <div className="caption">
          {data.chargingCurveIsMeasured
            ? <><span className="ok-text">Charge times from the measured pack curve.</span> Each stop costs at least {data.minStopMin.toFixed(0)} min.</>
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

export function TripReset() {
  const [sent, setSent] = useState<string | null>(null);
  const { data: ack } = usePoll(
    () => getJSON<{ ack: { applied?: boolean; action?: string } | null }>('/api/trip_reset/ack'), 5000, [sent ?? '']);
  return (
    <div className="card pad" style={{ marginTop: 8 }}>
      <div className="btnrow">
        <button className="btn" onClick={async () => {
          try {
            setSent((await postJSON<{ sentAt: string }>('/api/trip_reset', {})).sentAt);
            toast('Trip reset sent to the car');
          } catch (e) { toast(`Trip reset failed: ${e}`, 'err'); }
        }}><Icon name="history" size={13} />Reset trip</button>
        <span className="caption" style={{ margin: 0 }}>
          {sent
            ? (ack?.ack?.applied ? `Car confirmed — trip reset · sent ${sent}` : `Sent ${sent} — awaiting the car's confirmation.`)
            : "Zeroes the car's Trip distance. Does not touch the controller's odometer, lap count or energy."}
        </span>
      </div>
    </div>
  );
}
