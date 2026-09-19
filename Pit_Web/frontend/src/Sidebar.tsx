// Sidebar: race status, race control, overrides, cut lap, driver message,
// export, danger zone, appearance.

import { useState, type ReactNode } from 'react';
import { Icon } from './icons';
import { MISSING, fmt, getJSON, localInput, postJSON, usePoll } from './lib';
import { CarControls, DateTimeField, Disclosure, Pill } from './components';
import { StintPanel } from './DriverStint';
import { StartTimePanel } from './StartTime';
import { PublicNote } from './PublicNote';
import { SpectatorEstimate } from './SpectatorEstimate';
import { toast } from './toast';
import type { Config, Live } from './types';

// How the lap-command ack is polled: fast while a press is in flight, slow the
// rest of the time. See the poll in CutLap for why.
const ACK_FAST_MS = 1000;
const ACK_SLOW_MS = 5000;
const ACK_WATCH_MS = 20000;

function KV({ k, v, mono, wide }: { k: string; v: string; mono?: boolean; wide?: boolean }) {
  return (
    <div className={wide ? 'wide' : ''}>
      <div className="k">{k}</div>
      <div className={`v${mono ? ' mono' : ''}`}>{v}</div>
    </div>
  );
}

function Sec({ icon, title, children }: { icon: string; title: string; children: ReactNode }) {
  return <section className="sb-sec"><h4><Icon name={icon} size={13} />{title}</h4>{children}</section>;
}

export default function Sidebar({
  live, config, hidden, manualLap, setManualLap,
  fontScale, setFontScale, clockOffsetMs,
}: {
  live: Live | null; config: Config; hidden: boolean;
  manualLap: number; setManualLap: (n: number) => void;
  fontScale: number; setFontScale: (n: number) => void;
  clockOffsetMs: number;
}) {
  const [busy, setBusy] = useState(false);
  const race = live?.race;

  // Start sends NO startTime: the server stamps it with its own clock, so a
  // phone whose clock is minutes out cannot skew the race clock for everyone.
  // Resume passes the stored startTime back, the one case the client owns.
  const setRace = async (isRacing: boolean, startTime?: number | null) => {
    setBusy(true);
    try {
      // `newRace` comes back true only when this was a START, not a resume and
      // not a correction to the start time — the same test the server uses to
      // decide whether to zero the car. Say so either way: "the car kept its
      // warm-up laps" is something the pit has to find out AT the green flag,
      // not from a lap count that looks wrong twenty minutes later.
      const r = await postJSON<{ newRace?: boolean; carError?: string | null; carLinkDisabled?: boolean }>(
        '/api/race', startTime === undefined ? { isRacing } : { isRacing, startTime });
      if (r.carError && r.carLinkDisabled) {
        // Not a failure. This dashboard is not on the pit's store, so the
        // green flag deliberately stopped at the demo's own race clock and
        // the real car was never asked to zero anything.
        toast('Demo race clock started — the car was NOT reset, and nothing was sent to it.');
      } else if (r.carError) {
        toast(`Race started — but the car was not reached, so it is still counting its warm-up: ${r.carError}`, 'err');
      } else {
        toast(isRacing
          ? (r.newRace
            ? 'Race started — clock running, car reset to lap 0'
            : (startTime ? 'Race resumed' : 'Race started — clock running'))
          : 'Race stopped');
      }
    } catch (e) { toast(`Race clock: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  return (
    <aside className="sidebar" hidden={hidden}>
      <Sec icon="radio" title="Race status">
        <div className="kv">
          <KV k="Active lap" v={fmt(live?.activeLap ?? null, 'd')} />
          <KV k="Lap delta" v={fmt(live?.lapDelta ?? null, '+.1f')} mono />
          <KV k="Distance" v={live?.odometerKm == null ? MISSING : `${live.odometerKm.toFixed(1)} km`} />
          <KV k="Sector" v={live ? `S${live.sectorId} · ${live.sectorName}` : MISSING} wide />
          <KV k="Car is" v={ZONE_LABEL[live?.state?.zone ?? ''] ?? MISSING} />
          <KV k="Last lap" v={lastLapLabel(live?.state?.last_lap_kind ?? null,
                                           live?.state?.last_lap_stopped_s ?? null)} />
        </div>
      </Sec>

      <Sec icon="flag" title="Race control">
        <div className="btnrow">
          {race?.isRacing
            ? <button className="btn danger" disabled={busy} onClick={() => setRace(false, race.startTime)}>
                <Icon name="pause" size={13} />Stop race
              </button>
            : <button className="btn primary" disabled={busy} onClick={() => setRace(true)}>
                <Icon name="play" size={13} />Start race
              </button>}
          {race && !race.isRacing && race.startTime && (
            <button className="btn" disabled={busy} onClick={() => setRace(true, race.startTime)}>Resume</button>
          )}
        </div>
        <div className="caption">
          {race?.isRacing
            ? <>Running · {race.elapsedMin.toFixed(1)} min elapsed
                {race.startTime ? <> · started {new Date(race.startTime * 1000).toLocaleTimeString()}</> : null}</>
            : 'Not running · clock persists in SQLite'}
        </div>
        {/* Late to the laptop? The race clock can be set to when the race
            really began — see StartTime.tsx for why that is not cosmetic. */}
        <StartTimePanel race={race} stint={live?.driverStint} />
        <label className="fld">Manual lap override (−1 = use the car's count)</label>
        <input type="number" value={manualLap} onChange={(e) => setManualLap(Number(e.target.value))} />
      </Sec>

      <Sec icon="timer" title="Driver stint">
        <StintPanel stint={live?.driverStint} clockOffsetMs={clockOffsetMs}
                    racing={!!race?.isRacing} />
      </Sec>

      <CutLap carLap={live?.state?.auto_lap ?? null}
              lapHeld={live?.lapClock?.heldAt != null}
              carLink={!config.demoStore} />
      <DriverMessage carLink={!config.demoStore} />
      {/* What the PUBLIC sees while the car is silent. Its own file: see
          SpectatorEstimate.tsx for why it exists and what ends it. */}
      <Sec icon="radio" title="Spectator page">
        <SpectatorEstimate clockOffsetMs={clockOffsetMs} />
        {/* Why the car is stopped, in the crew's own words. PublicNote.tsx. */}
        <div style={{ height: 1, background: 'var(--line)', margin: '14px 0' }} />
        <PublicNote clockOffsetMs={clockOffsetMs} />
      </Sec>
      <ExportPanel config={config} />

      <Sec icon="sliders" title="Appearance">
        <label className="fld">Font scale · {fontScale.toFixed(2)}×</label>
        <input type="range" min={0.8} max={1.6} step={0.05} value={fontScale}
               onChange={(e) => setFontScale(Number(e.target.value))} />
      </Sec>

      <DangerZone race={race} />
    </aside>
  );
}

// What the CAR says, in the pit's words. A key the car did not send (an older
// car, or no GPS yet) has no entry and renders as the usual dash.
const ZONE_LABEL: Record<string, string> = {
  track: 'on track', pit_lane: 'in the pit lane', box: 'in the box',
};
const KIND_LABEL: Record<string, string> = {
  flying: 'flying', in: 'in-lap', out: 'out-lap', in_out: 'in + out',
  start: 'not from the line', suspect: 'suspect',
};

function lastLapLabel(kind: string | null, stoppedS: number | null): string {
  if (!kind) return MISSING;
  const label = KIND_LABEL[kind] ?? kind;
  return stoppedS !== null && stoppedS >= 10
    ? `${label} · stood ${Math.round(stoppedS)} s` : label;
}

function CutLap({ carLap, lapHeld, carLink }:
  { carLap: number | null; lapHeld: boolean; carLink: boolean }) {
  const [sent, setSent] = useState<string | null>(null);
  const [freshSent, setFreshSent] = useState<string | null>(null);
  const [setSentAt, setSetSentAt] = useState<string | null>(null);
  const [wanted, setWanted] = useState('');
  const [watchSent, setWatchSent] = useState<string | null>(null);
  const [holdSent, setHoldSent] = useState<string | null>(null);
  const [holdAction, setHoldAction] = useState('stop_stopwatch');
  // The id of the command each button last sent, so an ack can be matched to
  // the press it belongs to.
  const [sentIds, setSentIds] = useState<Record<string, number>>({});
  // WHEN THE ACK IS WORTH READING OFTEN. The car answers a press in a second or
  // two; at a flat 5 s poll the caption could sit on "awaiting the car's
  // confirmation" for most of another five after the answer was already there,
  // which reads in the pit as the CAR being slow. So the poll runs at 1 s for
  // the few seconds a press is actually in flight and drops back to 5 s after.
  //
  // The window is short and bounded by the press, not by the answer: an
  // unanswered command is unanswered because the car is not listening, and
  // polling it hard for minutes would not change that — it would just spend
  // every open device's network on it.
  const [pressedAt, setPressedAt] = useState(0);
  const watching = pressedAt > 0 && Date.now() - pressedAt < ACK_WATCH_MS;
  const { data: ack } = usePoll(
    () => getJSON<{ ack: { applied?: boolean; lap?: number; action?: string; id?: number } | null }>('/api/cut_lap/ack'),
    watching ? ACK_FAST_MS : ACK_SLOW_MS,
    [sent ?? '', freshSent ?? '', setSentAt ?? '', watchSent ?? '', holdSent ?? '']);

  // CONFIRMED MEANS THIS PRESS, NOT THIS BUTTON. The ack node is retained and
  // holds the last ack the car ever wrote, so with the car off it can be hours
  // old — matching on `action` alone made the sidebar answer "Car confirmed —
  // now on lap 9" to a Cut Lap press while the car had been dark for eight
  // hours, quoting a lap number from the morning. The id is the one the send
  // returned, so it can only match the command that press actually created.
  const applied = (what: string) =>
    !!ack?.ack?.applied && ack.ack.action === what
    && ack.ack.id != null && ack.ack.id === sentIds[what];

  const freshLap = async () => {
    try {
      const r = await postJSON<{ sentAt: string; id: number }>('/api/lap/restart', {});
      setPressedAt(Date.now());
      setSentIds((m) => ({ ...m, restart_lap: r.id }));
      setFreshSent(r.sentAt);
      toast('Restart lap sent — nothing will be counted');
    } catch (e) { toast(`Restart lap failed: ${e}`, "err"); }
  };

  const setLapNumber = async () => {
    const lap = Number(wanted);
    if (wanted.trim() === '' || !Number.isInteger(lap) || lap < 0) {
      toast('Type the lap count the car should show', 'err'); return;
    }
    try {
      const r = await postJSON<{ sentAt: string; id: number }>('/api/lap/set', { lap });
      setPressedAt(Date.now());
      setSentIds((m) => ({ ...m, set_lap: r.id }));
      setSetSentAt(r.sentAt);
      toast(`Lap number ${lap} sent — the tile moves when the car reports it`);
    } catch (e) { toast(`Set lap failed: ${e}`, "err"); }
  };

  // The DRIVER's stopwatch, not the pit's clock and not the lap count. Shares
  // the lap-command node, so it lands on the same ack and `action` is again
  // what says which press came back.
  // NO "restart" BUTTON HERE. Cut lap and Restart lap already re-datum the
  // driver's clock on the car (lap_tracker._trigger_lap sets the HUD's own
  // stopwatch datum), so a third button that only restarts it would be a
  // second way to do what the two above already did. Clearing it -- blanking
  // the clock to "--" until the next crossing -- is the one thing neither of
  // them does.
  // ONE stopwatch, two buttons: this and the one beside the driver's clock.
  // The wall parks immediately so the press feels like a press, and the car is
  // sent the same instruction. Display only at both ends -- no lap is cut, no
  // count moves -- and the next crossing of the line releases it unpressed.
  const toggleLapHold = async () => {
    const action = lapHeld ? 'resume_stopwatch' : 'stop_stopwatch';
    try {
      const r = await postJSON<{ sentAt: string | null; id: number | null; carError?: string }>(
        '/api/lap/hold', { hold: !lapHeld });
      setPressedAt(Date.now());
      if (r.id != null) setSentIds((m) => ({ ...m, [action]: r.id as number }));
      setHoldAction(action);
      setHoldSent(r.sentAt);
      toast(r.carError
        ? `Wall clock ${lapHeld ? 'running' : 'stopped'} — the car could not be reached`
        : (lapHeld ? 'Lap clock running again' : 'Lap clock stopped'), r.carError ? 'err' : undefined);
    } catch (e) { toast(`Lap clock failed: ${e}`, 'err'); }
  };

  const clearStopwatch = async () => {
    try {
      const r = await postJSON<{ sentAt: string; id: number }>('/api/lap/stopwatch', { action: 'clear' });
      setPressedAt(Date.now());
      setSentIds((m) => ({ ...m, clear_stopwatch: r.id }));
      setWatchSent(r.sentAt);
      toast("Clear stopwatch sent to the car");
    } catch (e) { toast(`Clear stopwatch failed: ${e}`, 'err'); }
  };

  return (
    <Sec icon="timer" title="Lap control">
      <CarControls enabled={carLink}>
      <button className="btn block" onClick={async () => {
        try {
          const r = await postJSON<{ sentAt: string; id: number }>('/api/cut_lap', {});
          setPressedAt(Date.now());
          setSentIds((m) => ({ ...m, cut_lap: r.id }));
          setSent(r.sentAt);
          toast('Cut Lap sent to the car');
        } catch (e) { toast(`Cut Lap failed: ${e}`, "err"); }
      }}><Icon name="flag" size={13} />Cut lap now</button>
      {sent && (
        <div className="caption">
          {applied('cut_lap')
            ? `Car confirmed — now on lap ${ack?.ack?.lap} · sent ${sent}`
            : `Sent ${sent} — awaiting the car's confirmation.`}
        </div>
      )}

      {/* Kept next to Cut lap because the two look alike and are not: this one
          records nothing. NOT a pit-stop button any more — the car closes the
          in-lap itself at the line in the pit lane and tags in/out laps, so
          they stay out of the energy figures without anyone pressing anything. */}
      <button className="btn block" style={{ marginTop: 8 }} onClick={freshLap}>
        <Icon name="timer" size={13} />Restart lap, don’t count it
      </button>
      {freshSent && (
        <div className="caption">
          {applied('restart_lap')
            ? `Car confirmed — fresh lap, still on lap ${ack?.ack?.lap} · sent ${freshSent}`
            : `Sent ${freshSent} — awaiting the car's confirmation. (A car on the old lap code never answers this.)`}
        </div>
      )}

      <div className="btnrow" style={{ marginTop: 8 }}>
        <input type="text" inputMode="numeric" placeholder={carLap === null ? 'lap' : String(carLap)}
               value={wanted} onChange={(e) => setWanted(e.target.value)} style={{ width: 72 }} />
        <button className="btn" onClick={setLapNumber}>Set car lap number</button>
      </div>
      {setSentAt && (
        <div className="caption">
          {applied('set_lap')
            ? `Car confirmed — now on lap ${ack?.ack?.lap} · sent ${setSentAt}`
            : `Sent ${setSentAt} — awaiting the car's confirmation.`}
        </div>
      )}

      {/* The driver's own clock. Kept at the bottom of this section because it
          is the one control here that records NOTHING — it moves a number on
          the driver's screen and leaves the lap count, the lap times, the
          energy and the odometer exactly where they were. */}
      <button className="btn block" style={{ marginTop: 8 }} onClick={clearStopwatch}>
        <Icon name="timer" size={13} />Clear driver’s stopwatch
      </button>
      {watchSent && (
        <div className="caption">
          {applied('clear_stopwatch')
            ? `Car confirmed — driver's clock blanked · sent ${watchSent}`
            : `Sent ${watchSent} — awaiting the car's confirmation. (A car on the old HUD code never answers this.)`}
        </div>
      )}

      {/* The pit wall's own lap clock, last because it is the only control in
          this section that touches nothing outside this building. */}
      <button className="btn block" style={{ marginTop: 8 }} onClick={toggleLapHold}>
        <Icon name={lapHeld ? 'play' : 'pause'} size={13} />
        {lapHeld ? 'Start lap clock' : 'Stop lap clock'}
      </button>
      {holdSent && (
        <div className="caption">
          {applied(holdAction)
            ? `Car confirmed — driver's clock ${holdAction === 'stop_stopwatch' ? 'stopped' : 'running'} · sent ${holdSent}`
            : `Sent ${holdSent} — awaiting the car's confirmation. (The wall is already ${holdAction === 'stop_stopwatch' ? 'stopped' : 'running'}.)`}
        </div>
      )}
      </CarControls>
    </Sec>
  );
}

function DriverMessage({ carLink }: { carLink: boolean }) {
  const [mode, setMode] = useState<'label' | 'text'>('label');
  const [label, setLabel] = useState('');
  const [num, setNum] = useState(0);
  const [text, setText] = useState('');
  const [last, setLast] = useState<string | null>(null);
  const ready = mode === 'text' ? text.trim().length > 0 : label.trim().length > 0;

  const send = async () => {
    const category = mode === 'text' ? '' : label.trim().toUpperCase();
    const value = mode === 'text' ? text.trim() : num;
    try {
      const r = await postJSON<{ shown: string }>('/api/driver_message', { category, value });
      setLast(`${r.shown} · ${new Date().toLocaleTimeString()}`);
      toast(`Sent to driver: ${r.shown}`);
    } catch (e) { toast(`Send failed: ${e}`, 'err'); }
  };

  return (
    <Sec icon="message" title="Driver message">
      <CarControls enabled={carLink}>
      <div className="chips">
        <button className="chip" aria-pressed={mode === 'label'} onClick={() => setMode('label')}>Label + number</button>
        <button className="chip" aria-pressed={mode === 'text'} onClick={() => setMode('text')}>Text</button>
      </div>
      {mode === 'text' ? (
        <>
          <label className="fld">Message</label>
          <input type="text" value={text} placeholder="e.g. PIT IN NOW"
                 onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && ready && send()} />
        </>
      ) : (
        <>
          <label className="fld">Label</label>
          <input type="text" value={label} placeholder="e.g. Wanted Speed" onChange={(e) => setLabel(e.target.value)} />
          <label className="fld">Number</label>
          <input type="number" value={num} onChange={(e) => setNum(Number(e.target.value))}
                 onKeyDown={(e) => e.key === 'Enter' && ready && send()} />
        </>
      )}
      <div className="btnrow" style={{ marginTop: 10 }}>
        <button className="btn primary" disabled={!ready} onClick={send}><Icon name="send" size={13} />Send</button>
        <button className="btn" onClick={async () => {
          try { await postJSON('/api/driver_message', {}, 'DELETE'); setLast(null); toast('Driver message cleared'); }
          catch (e) { toast(`Clear failed: ${e}`, 'err'); }
        }}>Clear</button>
      </div>
      <div className="caption">{last ? `Now showing: ${last}` : 'Nothing on the driver HUD right now.'}</div>
      </CarControls>
    </Sec>
  );
}

/** Fetch a workbook and hand it to the browser as a download.
 *
 *  Shared by both export buttons so there is ONE place that reads the server's
 *  filename, turns the body into a blob and revokes the object URL. The two
 *  differ only in the URL they ask for and what they say afterwards.
 */
async function saveWorkbook(url: string) {
  const res = await fetch(url);
  if (!res.ok) {
    // The per-lap export answers a bad lap range with a 400 and a sentence
    // worth showing ("28 laps in that range; 300 sheets is the limit"). A bare
    // status code would send the crew to the server log for something they can
    // fix in the field.
    const why = await res.json().then((b) => b?.detail).catch(() => null);
    throw new Error(why || `server said ${res.status}`);
  }
  const rows = Number(res.headers.get('X-Row-Count') || 0);
  const laps = Number(res.headers.get('X-Lap-Count') || 0);
  const cd = res.headers.get('content-disposition') || '';
  const name = /filename="([^"]+)"/.exec(cd)?.[1] ?? 'telemetry.xlsx';
  const blob = await res.blob();
  const href = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = href;
  link.download = name;
  link.click();
  URL.revokeObjectURL(href);
  return { name, size: blob.size, rows, laps };
}

/** The per-lap workbook: one sheet per lap, beside the time-ranged one.
 *
 *  Its own section because it answers a different question — "show me lap 87",
 *  not "show me 14:00 to 15:00" — and because the lap count behind its two
 *  fields is a whole-table group that should not run until someone opens this.
 *  The system chips above are shared: both workbooks carry the same columns.
 */
function PerLapExport({ groups, busy: otherBusy }: { groups: string[]; busy: boolean }) {
  const [open, setOpen] = useState(false);
  const [first, setFirst] = useState('');
  const [last, setLast] = useState('');
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [done, setDone] = useState<string | null>(null);

  // Only once the section is opened — see the endpoint's docstring.
  const { data } = usePoll(
    () => getJSON<{ firstLap: number | null; lastLap: number | null; laps: number; maxSheets: number }>(
      '/api/export/lap_bounds'), 300000, [], open);

  const lo = data?.firstLap ?? null;
  const hi = data?.lastLap ?? null;
  const a = first === '' ? lo : Number(first);
  const b = last === '' ? hi : Number(last);
  const bad = a === null || b === null || !Number.isInteger(a) || !Number.isInteger(b) || a > b;
  const count = bad ? 0 : (data?.laps ? Math.min(b - a + 1, data.laps) : 0);
  const over = !!(data && count > data.maxSheets);

  const download = async () => {
    setBusy(true);
    setDone(null);
    setElapsed(0);
    const t0 = Date.now();
    const tick = window.setInterval(() => setElapsed((Date.now() - t0) / 1000), 200);
    try {
      const { name, size, rows, laps } = await saveWorkbook(
        `/api/export/laps.xlsx?firstLap=${a}&lastLap=${b}&groups=${encodeURIComponent(groups.join(','))}`);
      setDone(`${name} · ${(size / 1e6).toFixed(1)} MB · ${laps} laps, ${rows.toLocaleString()} rows in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
    } catch (e) {
      toast(`Per-lap export failed: ${e}`, 'err');
    } finally {
      clearInterval(tick);
      setBusy(false);
    }
  };

  return (
    <Disclosure icon="flag" title="Per lap — one sheet per lap" onToggle={setOpen}>
      <div className="caption" style={{ marginTop: 0 }}>
        {!data
          ? 'Counting laps…'
          : data.laps
            ? `Laps ${lo}–${hi} stored. A tab per lap: its summary, then every sample it holds. Same systems as above.`
            : 'No completed lap stored yet.'}
      </div>
      {data && data.laps ? <>
        <div className="btnrow" style={{ marginTop: 8 }}>
          <input type="text" inputMode="numeric" placeholder={String(lo)} aria-label="First lap"
                 value={first} onChange={(e) => setFirst(e.target.value)} style={{ width: 72 }} disabled={busy} />
          <span className="caption">to</span>
          <input type="text" inputMode="numeric" placeholder={String(hi)} aria-label="Last lap"
                 value={last} onChange={(e) => setLast(e.target.value)} style={{ width: 72 }} disabled={busy} />
        </div>
        <button className="btn block" style={{ marginTop: 10 }}
                disabled={busy || otherBusy || bad || over || !groups.length} onClick={download}>
          {busy
            ? <><span className="spinner" aria-hidden="true" />Building… {elapsed.toFixed(0)} s</>
            : <><Icon name="download" size={13} />Download per-lap workbook</>}
        </button>
        <div className="caption" aria-live="polite">
          {busy
            ? `Building ${count} lap${count === 1 ? '' : 's'} — a sheet each, so this takes longer than the workbook above. Nothing arrives until it is finished.`
            : done
              ? `Saved ${done}`
              : bad
                ? 'Give a first lap and a last lap, first one no higher than the last.'
                : over
                  ? `${count} laps is past the ${data.maxSheets}-sheet limit. Narrow the range.`
                  : !groups.length
                    ? 'Pick at least one system above.'
                    : `${count} lap${count === 1 ? '' : 's'} in this range, one sheet each.`}
        </div>
      </> : null}
    </Disclosure>
  );
}

function ExportPanel({ config }: { config: Config }) {
  const { data } = usePoll(
    () => getJSON<{ lo: number | null; hi: number | null; total: number }>('/api/export/bounds'), 60000);
  const [groups, setGroups] = useState<string[]>(config.exportGroups);
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [done, setDone] = useState<string | null>(null);

  const lo = data?.lo ?? null;
  const hi = data?.hi ?? lo;
  const a = from || (lo === null ? '' : localInput(lo));
  const b = to || (hi === null ? '' : localInput(hi));
  const startS = a ? new Date(a).getTime() / 1000 : null;
  const endS = b ? new Date(b).getTime() / 1000 : null;

  // What this range costs, asked BEFORE committing to it. Every hook has to run
  // above the empty-store return below; this panel is the easiest place in the
  // app to introduce a conditional hook by accident.
  const { data: est } = usePoll(
    () => (startS === null || endS === null
      ? Promise.resolve({ rows: 0 })
      : getJSON<{ rows: number }>(
        `/api/export/estimate?start=${startS}&end=${endS}`)),
    60000, [startS, endS]);

  if (!data || !data.total || lo === null) {
    return <Sec icon="download" title="Export"><div className="caption">No telemetry stored yet — start collector.py.</div></Sec>;
  }

  const url = `/api/export/telemetry.xlsx?start=${startS}&end=${endS}`
    + `&groups=${encodeURIComponent(groups.join(','))}`;

  // fetch, not a link. A link hands the wait to the browser, which shows
  // nothing until the file lands; this way the panel can say it is working,
  // and can refuse a second click that would start a second export.
  const download = async () => {
    setBusy(true);
    setDone(null);
    setElapsed(0);
    const t0 = Date.now();
    const tick = window.setInterval(() => setElapsed((Date.now() - t0) / 1000), 200);
    try {
      const { name, size, rows } = await saveWorkbook(url);
      setDone(`${name} · ${(size / 1e6).toFixed(1)} MB · ${rows.toLocaleString()} rows in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
    } catch (e) {
      toast(`Export failed: ${e}`, 'err');
    } finally {
      clearInterval(tick);
      setBusy(false);
    }
  };

  return (
    <Sec icon="download" title="Export (Excel)">
      <div className="caption" style={{ marginTop: 0 }}>{data.total.toLocaleString()} samples stored</div>
      <div className="chips">
        {config.exportGroups.map((g) => (
          <button key={g} className="chip" aria-pressed={groups.includes(g)} disabled={busy}
                  onClick={() => setGroups((s) => s.includes(g) ? s.filter((x) => x !== g) : [...s, g])}>{g}</button>
        ))}
      </div>
      <label className="fld">From</label>
      <DateTimeField label="From" value={a} min={localInput(lo)} max={localInput(hi ?? lo)}
                     disabled={busy} onChange={setFrom} />
      <label className="fld">To</label>
      <DateTimeField label="To" value={b} min={localInput(lo)} max={localInput(hi ?? lo)}
                     disabled={busy} onChange={setTo} />
      <button className="btn primary block" style={{ marginTop: 10 }}
              disabled={busy || !groups.length} onClick={download}>
        {busy
          ? <><span className="spinner" aria-hidden="true" />Building… {elapsed.toFixed(0)} s</>
          : <><Icon name="download" size={13} />Download workbook</>}
      </button>
      {/* aria-live so the wait is announced, not just drawn. The row count is
          exact; no duration is offered, because measured over HTTP the same
          export ranged 5.04-14.30 s run to run and a smaller one sometimes
          took longer than a bigger one. */}
      <div className="caption" aria-live="polite">
        {busy
          ? `Building ${est && est.rows ? `${est.rows.toLocaleString()} rows` : 'the workbook'} — nothing arrives until it is finished. Leave this open.`
          : done
            ? `Saved ${done}`
            : !groups.length
              ? 'Pick at least one system to include.'
              : est && est.rows
                ? `${est.rows.toLocaleString()} rows in this range. How long it takes varies; the button counts the seconds.`
                : 'No samples in this range.'}
      </div>
      {/* Added BESIDE the workbook above, never in place of it: the time-ranged
          export is what the crew downloads at the end of a session and it is
          left exactly as it was. */}
      <PerLapExport groups={groups} busy={busy} />
    </Sec>
  );
}

/** clear_history is irreversible and sits behind a typed phrase, not a click.
 *  In Streamlit it was a popover plus a confirm; here the phrase is the guard,
 *  and the server refuses anything else. */
function DangerZone({ race }: { race: Live['race'] | undefined }) {
  const PHRASE = 'DELETE HISTORY';
  const [typed, setTyped] = useState('');
  const [busy, setBusy] = useState(false);
  const [resetting, setResetting] = useState(false);

  // What a reset would throw away, so the button can say it before you press
  // it rather than after. A start time cannot be reconstructed by hand.
  const started = race?.startTime != null;
  const mins = race?.elapsedMin ?? 0;
  const ran = mins >= 60
    ? `${Math.floor(mins / 60)} h ${Math.round(mins % 60)} m`
    : `${Math.round(mins)} min`;

  return (
    <Disclosure icon="trash" title="Danger zone">
      {/* Started a race by accident? "Stop race" keeps the start time so
          Resume works, so this is the only way back to "never started". */}
      <h4 style={{ marginBottom: 8 }}>Race clock</h4>
      {!started
        ? <div className="caption" style={{ marginTop: 0 }}>No race clock to reset — nothing has been started.</div>
        : <>
            <Pill kind="warn">
              Clears the race clock and the driver stint, putting both back to “never started”.
              This race has run <b>{ran}</b>{race?.isRacing ? ' and is RUNNING' : ''}. Reversible for two minutes.
            </Pill>
            <button className="btn danger block" disabled={busy || resetting}
                    onClick={async () => {
                      setResetting(true);
                      try {
                        const r = await postJSON<{ clearedElapsedMin: number; clearedStint: number }>(
                          '/api/race/reset', {});
                        toast(
                          `Race clock reset — cleared a race that had run ${Math.round(r.clearedElapsedMin)} min`
                          + (r.clearedStint ? ` and ${r.clearedStint} stint${r.clearedStint > 1 ? 's' : ''}` : '')
                          + '. Undo below for 2 minutes.', 'info');
                      } catch (e) { toast(`Race reset failed: ${e}`, 'err'); }
                      finally { setResetting(false); }
                    }}>
              <Icon name="history" size={13} />Reset race clock
            </button>
          </>}
      {race?.canUndo && (
        <button className="btn block" style={{ marginTop: 8 }} disabled={busy}
                onClick={async () => {
                  try {
                    await postJSON('/api/race/reset/undo', {});
                    toast('Race clock restored — start time and stint are back', 'info');
                  } catch (e) { toast(`Undo failed: ${e}`, 'err'); }
                }}>
          <Icon name="history" size={13} />Undo race reset
        </button>
      )}

      <h4 style={{ margin: '18px 0 8px' }}>Telemetry</h4>
      <Pill kind="warn">Reset history deletes every stored sample for this device. It cannot be undone. Export first.</Pill>
      <label className="fld">Type <code>{PHRASE}</code> to enable</label>
      <input type="text" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={PHRASE} autoComplete="off" />
      <button className="btn danger block" style={{ marginTop: 10 }} disabled={typed !== PHRASE || busy}
              onClick={async () => {
                setBusy(true);
                try {
                  const r = await postJSON<{ deleted: number }>('/api/clear_history', { confirm: typed });
                  toast(`History cleared — ${r.deleted.toLocaleString()} samples deleted`, 'info');
                  setTyped('');
                } catch (e) { toast(`Clear failed: ${e}`, 'err'); }
                finally { setBusy(false); }
              }}>
        <Icon name="trash" size={13} />Reset history
      </button>
    </Disclosure>
  );
}
