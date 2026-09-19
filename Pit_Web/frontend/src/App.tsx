// The pit wall shell: app bar, always-visible top strip, then the five tabs.

import { useEffect } from 'react';
import Sidebar from './Sidebar';
import Driver from './tabs/Driver';
import History from './tabs/History';
import { Cells, LiveMetrics, Strategy, Weather } from './tabs/Rest';
import { ChargingBadge, FaultBanner, MetricTile, PowerMapBadge } from './components';
import { ErrorBoundary } from './ErrorBoundary';
import { StintBanner, StintClock, SwapBanner, stintNow } from './DriverStint';
import { ChargeClock } from './ChargeClock';
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
// How far into a lap the energy baseline may sit before the Current-lap figure
// is meaningfully short. A sample lands every ~0.5 s, so a healthy baseline is
// a few metres in; 100 m means the pit missed the start of the lap outright.
const LAP_ENERGY_BASELINE_MAX_M = 100;

/** Says when Current lap energy is measured from partway into the lap, so a
 *  figure that is short because of a dropped link never passes for a low one.
 *  Same rule as an assumed speed profile: never let a gap look like a reading. */
function lapEnergyNote(live: Live): string | null {
  const from = live.currentLapEnergyFromM;
  if (live.currentLapEnergy === null || from === null) return null;
  return from > LAP_ENERGY_BASELINE_MAX_M
    ? `from ${Math.round(from)} m in — start of lap not received`
    : null;
}

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
      <ChargingBadge state={s} />
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
      <div className="grid g4" style={{ marginTop: 12 }}>
        <MetricTile title="Last lap time" value={lapTime(s.last_lap_time_s)} unit={`m:ss${lapSrc}`} />
        <MetricTile title="Last lap energy" value={fmt(s.last_lap_energy, '.1f')} unit="Wh" />
        {/* Next to Last lap energy on purpose: the pair is "am I spending more
            than the lap that just worked?", and that only reads at a glance
            when the two numbers are side by side. Both are net of regen. */}
        <MetricTile title="Current lap energy" value={fmt(live.currentLapEnergy, '.1f')} unit="Wh"
                    note={lapEnergyNote(live)} />
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

/** The driver's lap clock, on the pit wall.
 *
 *  Counts from the instant the car says the lap began, so it matches the
 *  stopwatch on the HUD rather than being a second, slightly different
 *  measurement of the same thing.
 *
 *  IT KEEPS RUNNING WHEN THE CAR GOES QUIET. It used to freeze on the last
 *  sample, on the argument that a clock counting through a dead link reports a
 *  long lap rather than a dead link. That argument holds for a MEASUREMENT and
 *  not for a stopwatch: the lap did not pause because the telemetry did, and on
 *  this car the link drops often enough that a clock which stops whenever it
 *  does is a clock nobody can use. The pit asked for one that always runs.
 *
 *  What the old freeze was protecting is kept, in the label rather than in the
 *  number: while the car is quiet the clock says so and how long for, so the
 *  figure is never read as something the car has confirmed. The pit's own Stop
 *  button still parks it — that is a deliberate press, not a missing link. */
function LapClock({ live, link, clockOffsetMs }: {
  live: Live | null | undefined; link: Link; clockOffsetMs: number;
}) {
  const lc = live?.lapClock;
  const started = lc?.startedAt ?? null;
  // Parked by the pit (Stop lap clock). Freezing on the instant of the press
  // rather than on the newest sample matters at the finish: the car is still
  // sending, so "the last sample" would keep moving under a stopped clock.
  const parked = lc?.heldAt ?? null;
  // The ONLY thing that stops it now.
  const ticking = parked === null;
  const serverNowS = useAlignedServerNow(clockOffsetMs, ticking ? started : null);
  const elapsed = started === null ? null
    : parked !== null ? parked - started
    : serverNowS - started;
  const held = !!started && parked !== null;
  // Running, but on the pit's clock alone: nothing has come from the car since
  // `live.age`. lapClock.atSampleS is what the car last confirmed, and it goes
  // in the tooltip so the two numbers are both available without the big one
  // pretending to be the smaller.
  // `link`, not live.fresh: fresh is the SERVER's verdict at the moment it
  // pushed, so with the socket down it would go on saying the car is live for
  // as long as the link stayed down. linkState already folds in the socket and
  // the silence, and it is what the badge beside this clock reads.
  const quiet = !!started && !held && link !== 'live';
  // How old the newest sample is NOW, counted here. live.age is the age AT THE
  // PUSH and freezes with it, so a dead socket would leave this clock claiming
  // "no data 4s" for the rest of the afternoon.
  const sampleAt = live && live.age != null ? live.ts - live.age : null;
  const quietFor = sampleAt !== null ? Math.max(0, serverNowS - sampleAt) : null;
  const confirmed = lc?.atSampleS;
  const datum = lc?.source === 'store'
    ? 'Estimated from the earliest sample the pit holds for this lap — it can read short. The car itself reports the exact datum once it is running code that sends lap_started_ts.'
    : lc?.source === 'pit'
    ? "Counting from the press in this room. The car has not answered it yet; the clock hands back to the car's own datum at the next sample."
    : "The car's own lap datum — the same one the driver's stopwatch counts from.";
  return (
    <div className={`clock lap${held ? ' held' : ''}${quiet ? ' quiet' : ''}`}
         title={quiet
           ? `Still counting, on the pit's clock. Nothing has arrived from the car for ${ageText(quietFor)}`
             + (confirmed != null ? `; the last figure it confirmed was ${hms(Math.max(0, Math.floor(confirmed)))}` : '')
             + `. ${datum}`
           : datum}>
      <span className="k">
        {held ? 'Lap clock · held'
          : quiet ? `Lap clock · no data ${ageText(quietFor)}`
          : 'Lap clock'}
      </span>
      <span className="v">{elapsed === null ? '--:--' : hms(Math.max(0, Math.floor(elapsed)))}</span>
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


  // ?tab=Strategy opens straight on that tab, whatever this browser last had
  // open — a launcher can point somebody at one screen without them hunting
  // for it. Once only: after this the tab is the operator's, and re-applying
  // it on every render would make the other tabs unclickable.
  useEffect(() => {
    const want = new URLSearchParams(window.location.search).get('tab');
    if (want && TAB_NAMES.includes(want)) setTab(want as Tab);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
          <ChargeClock charge={live?.charge} clockOffsetMs={clockOffsetMs} />
          <span className="clock-sep" />
          <LapClock live={live} link={link} clockOffsetMs={clockOffsetMs} />
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
          {/* The single most important thing to know about this page, so it
              is the first thing on it and it never goes away. A demo backend
              reads a different SQLite file, but Firebase has only one car and
              one spectator page — see api.car_link() for what is blocked. */}
          {config.demoStore && (
            <div className="banner demo" role="status">
              <Icon name="sliders" size={16} />
              <span>
                DEMO — not the pit's store, and not connected to the car. Nothing here
                can command the car or change the public spectator page. Every reading
                below is demo data.
              </span>
            </div>
          )}
          {frozenSince && (
            // The server's `fresh` flag cannot say this: it stops arriving too.
            <div className="banner conn" role="alert">
              <Icon name="wifioff" size={16} />
              <span>CONNECTION LOST — every number on this page is frozen at {frozenSince}. Reconnecting…</span>
            </div>
          )}
          <SwapBanner stint={live?.driverStint} clockOffsetMs={clockOffsetMs} />
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
          {tab === 'Strategy' && <ErrorBoundary name="Strategy"><Strategy config={config} manualLap={manualLap} setManualLap={setManualLap} dark={dark}
                     selected={live?.activeProfile?.source === 'pit' ? live.activeProfile.key : undefined} /></ErrorBoundary>}
        </main>
      </div>
      <ToastHost />
    </div>
  );
}
