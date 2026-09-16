// The pit wall shell: app bar, always-visible top strip, then the five tabs.

import { useEffect } from 'react';
import Sidebar from './Sidebar';
import Driver from './tabs/Driver';
import History from './tabs/History';
import { Cells, LiveMetrics, Strategy, Weather } from './tabs/Rest';
import { FaultBanner, MetricTile, PowerMapBadge } from './components';
import { ErrorBoundary } from './ErrorBoundary';
import { StintBanner, StintClock, stintNow } from './DriverStint';
import { Icon } from './icons';
import { MISSING, ageText, fmt, hms, lapTime, useAlignedServerNow, useConfig, useLive, useStored } from './lib';
import { useSparklines } from './Sparkline';
import { ToastHost } from './toast';
import type { Live } from './types';

const TABS = [
  { name: 'Driver Telemetry', icon: 'gauge' },
  { name: 'Live Metrics', icon: 'activity' },
  { name: 'Cell Voltages', icon: 'battery' },
  { name: 'History', icon: 'history' },
  { name: 'Weather', icon: 'sun' },
  { name: 'Strategy', icon: 'route' },
] as const;
type Tab = (typeof TABS)[number]['name'];
const TAB_NAMES: readonly string[] = TABS.map((t) => t.name);

/** How long the socket may go quiet before the numbers are declared frozen.
 *  The server pushes every 2 s, so 10 s is five missed ticks. */
const FROZEN_AFTER_S = 10;

// Which History metric backs each strip tile's sparkline (metrics.py keys).
// No BattTemp: the tile shows the hottest Orion cell, while the stored
// BattTemp series is the thermistor module's average, a different quantity.
const STRIP_TREND = ['Speed', 'MotorTemp', 'CtrlTemp', 'SoC', 'Power', 'Energy'];

/** The seven-tile strip plus the three lap tiles. Tiers are computed in
 *  Python by limits.classify(); nothing here compares against a threshold. */
function TopStrip({ live }: { live: Live }) {
  const s = live.state;
  const t = live.tiers;
  const trend = useSparklines(STRIP_TREND);
  const ohms = s.motor_ohms === null ? '' : ` · ${s.motor_ohms.toFixed(1)} Ω`;
  const lapSrc = s.lap_source && !['gps', 'manual'].includes(s.lap_source) ? ` · ${s.lap_source}` : '';

  return (
    <>
      <FaultBanner live={live} />
      <PowerMapBadge state={s} />
      <div className="grid g7">
        <MetricTile title="Speed" value={fmt(s.speed_kmh, '.1f')} unit="km/h" trend={trend.Speed} />
        {s.motor_temp === null
          ? <MetricTile title="Motor temp" value={MISSING} unit={ohms.replace(' · ', '') || 'no sensor'} missing />
          : <MetricTile title="Motor temp" value={s.motor_temp.toFixed(1)} unit={`°C${ohms}`} tier={t.motorTemp} trend={trend.MotorTemp} />}
        <MetricTile title="Controller temp" value={fmt(s.temp)} unit="°C" tier={t.ctrlTemp} trend={trend.CtrlTemp} />
        <MetricTile title="Battery SoC" value={fmt(s.soc)} unit="%" tier={t.soc} trend={trend.SoC} />
        <MetricTile title="Battery temp" value={fmt(s.batt_temp)} unit="°C" tier={t.battTemp} />
        <MetricTile title="Power out" value={fmt(s.power_w)} unit="W" tier={t.power} trend={trend.Power} />
        <MetricTile title="Lap distance" value={fmt(live.lapDistanceM)} unit="m" />
      </div>
      <div className="grid g3" style={{ marginTop: 12 }}>
        <MetricTile title="Last lap time" value={lapTime(s.last_lap_time_s)} unit={`m:ss${lapSrc}`} />
        <MetricTile title="Last lap energy" value={fmt(s.last_lap_energy, '.1f')} unit="Wh" />
        <MetricTile title="Total race energy" value={fmt(s.total_race_energy, '.1f')} unit="Wh net" trend={trend.Energy} />
      </div>
    </>
  );
}

type Link = 'connecting' | 'live' | 'stale' | 'nodata' | 'frozen' | 'down';

function linkState(live: Live | null, connected: boolean, silentS: number): Link {
  if (!connected) return 'down';
  if (!live) return 'connecting';
  if (silentS > FROZEN_AFTER_S) return 'frozen';
  if (live.fresh) return 'live';
  if (live.age === null) return 'nodata';
  return 'stale';
}

function StatusPill({ link, live, silentS }: { link: Link; live: Live | null; silentS: number }) {
  switch (link) {
    case 'down': return <span className="status down"><Icon name="wifioff" size={13} />socket down</span>;
    case 'connecting': return <span className="status neutral"><span className="dot" />connecting</span>;
    case 'frozen': return <span className="status down"><Icon name="wifioff" size={13} />FROZEN · {Math.round(silentS)}s silent</span>;
    case 'live':
      // The feed is live. That now means the PI is alive -- it heartbeats
      // whether or not CAN and GPS are working -- so a live badge no longer
      // implies the car's sensors are talking. Say so when they are not, in a
      // warning colour not an error: the Pi being alive is genuinely better
      // news than the alternative. Stay out of the way when they are.
      if (live?.health && !live.health.ok) {
        return <span className="status stale" title={live.health.gpsDetail ?? undefined}>
          <Icon name="radio" size={13} />Pi alive {Math.round(live.age ?? 0)}s ago · {live.health.problems.join(' · ')}
        </span>;
      }
      return <span className="status live"><span className="dot" />LIVE · {Math.round(live?.age ?? 0)}s</span>;
    case 'nodata': return <span className="status down"><Icon name="alert" size={13} />no data — collector?</span>;
    default: return <span className="status stale"><Icon name="alert" size={13} />STALE · {ageText(live?.age ?? null)}</span>;
  }
}

/** The race countdown, in its own component so it can align its re-render to
 *  the race's own second boundary. See useAlignedServerNow. */
function RaceClock({ race, clockOffsetMs }: { race: Live['race'] | undefined; clockOffsetMs: number }) {
  const running = !!(race?.isRacing && race.startTime);
  const serverNowS = useAlignedServerNow(clockOffsetMs, running ? (race!.startTime as number) : null);
  const remaining = running ? Math.max(0, 24 * 3600 - (serverNowS - (race!.startTime as number))) : 24 * 3600;
  return (
    <div className={`clock${race?.isRacing ? ' running' : ''}`}>
      <span className="k">{race?.isRacing ? 'Time remaining' : 'Race not started'}</span>
      <span className="v">{race ? hms(remaining) : '--:--:--'}</span>
    </div>
  );
}

export default function App() {
  const { config, error } = useConfig();
  const [tab, setTab] = useStored<Tab>('pit.tab', 'Driver Telemetry', (v) => TAB_NAMES.includes(v));
  const [dark, setDark] = useStored('pit.dark', true);
  const [fontScale, setFontScale] = useStored('pit.font', 1);
  const [manualLap, setManualLap] = useStored('pit.manualLap', -1);
  const [sidebarOpen, setSidebarOpen] = useStored('pit.sidebar', true);
  const { live, connected, silentS, clockOffsetMs } = useLive(manualLap);
  const link = linkState(live, connected, silentS);


  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.documentElement.style.setProperty('--pit-font-scale', String(fontScale));
  }, [dark, fontScale]);

  // Tier colours are injected from limits.py so the pit renders a breach in the
  // same colour the driver sees. Light mode gets the darker variants.
  useEffect(() => {
    if (!config) return;
    const src = dark ? config.tierColours : config.tierColoursLight;
    const root = document.documentElement;
    if (src.warning) root.style.setProperty('--pit-warning', src.warning);
    if (src.critical) root.style.setProperty('--pit-critical', src.critical);
  }, [config, dark]);

  // The tab title carries the link state, so a pit engineer with six tabs open
  // sees "FROZEN" without switching to this one.
  useEffect(() => {
    const word = { live: 'LIVE', stale: 'STALE', frozen: 'FROZEN', down: 'OFFLINE', nodata: 'NO DATA', connecting: '…' }[link];
    const faults = live?.faults.length ? ` · ${live.faults.length} FAULT${live.faults.length > 1 ? 'S' : ''}` : '';
    const sn = stintNow(live?.driverStint, (Date.now() + clockOffsetMs) / 1000);
    // A driver change you are late for belongs in the tab title, where it is
    // visible from another window. Only while the clock is actually running:
    // a held countdown is not something to shout about.
    const stint = !sn.running || sn.remaining === null ? ''
      : sn.remaining < 0 ? ' · DRIVER CHANGE OVERDUE'
      : sn.remaining <= (live?.driverStint.warnS ?? 0) ? ' · DRIVER CHANGE DUE' : '';
    document.title = `${word}${faults}${stint} · Afeka Pit Wall`;
  }, [link, live?.faults.length, live?.driverStint, clockOffsetMs]);

  if (error) return (
    <main style={{ padding: 32, maxWidth: 560 }}>
      <h1 style={{ fontSize: 'calc(20px * var(--pit-font-scale))', marginBottom: 8 }}>Cannot reach the backend</h1>
      <p className="caption">{error}</p>
      <p className="caption">Is uvicorn running? <code>python -m uvicorn Pit_Web.api:app --port 8000</code></p>
    </main>
  );
  if (!config) return <main style={{ padding: 32 }} className="caption">loading configuration…</main>;

  const race = live?.race;
  const frozenSince = link === 'frozen' || link === 'down'
    ? new Date(Date.now() - silentS * 1000).toLocaleTimeString() : null;

  return (
    <div className="shell">
      <header className="bar">
        <button className="iconbtn" aria-pressed={sidebarOpen} title="Toggle controls"
                onClick={() => setSidebarOpen(!sidebarOpen)}>
          <Icon name="panel" size={17} />
        </button>
        <div className="brand">
          <div className="brand-mark"><Icon name="zap" size={17} /></div>
          <div>
            <div className="brand-name">AFEKA PIT WALL</div>
            <div className="brand-sub">Circuit Zolder · 24h</div>
          </div>
        </div>
        <div className="bar-center">
          <RaceClock race={race} clockOffsetMs={clockOffsetMs} />
          <span className="clock-sep" />
          <StintClock stint={live?.driverStint} clockOffsetMs={clockOffsetMs} />
        </div>
        <div className="bar-right">
          <StatusPill link={link} live={live} silentS={silentS} />
          <button className="iconbtn" title={dark ? 'Light mode' : 'Dark mode'} onClick={() => setDark(!dark)}>
            <Icon name={dark ? 'sun' : 'moon'} size={17} />
          </button>
        </div>
      </header>

      <div className="body">
        <Sidebar live={live} config={config} hidden={!sidebarOpen}
                 manualLap={manualLap} setManualLap={setManualLap}
                 fontScale={fontScale} setFontScale={setFontScale}
                 clockOffsetMs={clockOffsetMs} />
        <main className="main">
          {frozenSince && (
            // The server's `fresh` flag cannot say this: it stops arriving too.
            <div className="banner conn" role="alert">
              <Icon name="wifioff" size={16} />
              <span>CONNECTION LOST — every number on this page is frozen at {frozenSince}. Reconnecting…</span>
            </div>
          )}
          <StintBanner stint={live?.driverStint} clockOffsetMs={clockOffsetMs} />
          {live ? <TopStrip live={live} /> : <div className="caption">waiting for the first sample…</div>}

          <div className="tabs" role="tablist">
            {TABS.map((t) => (
              <button key={t.name} role="tab" className="tab" aria-selected={tab === t.name}
                      onClick={() => setTab(t.name)}>
                <Icon name={t.icon} size={15} />{t.name}
              </button>
            ))}
          </div>

          {/* History stays MOUNTED across tab switches: unmounting would purge
              the Plotly instance and throw away the zoom. */}
          <div hidden={tab !== 'History'}>
            <ErrorBoundary name="History"><History config={config} dark={dark} visible={tab === 'History'}
                     fresh={!!live?.fresh} age={live?.age ?? null} /></ErrorBoundary>
          </div>
          {tab === 'Driver Telemetry' && live && <ErrorBoundary name="Driver Telemetry"><Driver live={live} config={config} /></ErrorBoundary>}
          {tab === 'Live Metrics' && live && <ErrorBoundary name="Live Metrics"><LiveMetrics live={live} config={config} /></ErrorBoundary>}
          {tab === 'Cell Voltages' && <ErrorBoundary name="Cell Voltages"><Cells /></ErrorBoundary>}
          {tab === 'Weather' && <ErrorBoundary name="Weather"><Weather dark={dark} /></ErrorBoundary>}
          {tab === 'Strategy' && <ErrorBoundary name="Strategy"><Strategy config={config} manualLap={manualLap} dark={dark} /></ErrorBoundary>}
        </main>
      </div>
      <ToastHost />
    </div>
  );
}
