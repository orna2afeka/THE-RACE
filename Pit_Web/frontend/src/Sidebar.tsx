// Sidebar: race status, race control, overrides, cut lap, driver message,
// export, danger zone, appearance.

import { useState, type ReactNode } from 'react';
import { Icon } from './icons';
import { MISSING, fmt, getJSON, localInput, postJSON, usePoll } from './lib';
import { Disclosure, Pill } from './components';
import { StintPanel } from './DriverStint';
import { StartTimePanel } from './StartTime';
import { toast } from './toast';
import type { Config, Live } from './types';

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
      await postJSON('/api/race', startTime === undefined ? { isRacing } : { isRacing, startTime });
      toast(isRacing ? (startTime ? 'Race resumed' : 'Race started — clock running') : 'Race stopped');
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

      <CutLap />
      <DriverMessage />
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

function CutLap() {
  const [sent, setSent] = useState<string | null>(null);
  const { data: ack } = usePoll(
    () => getJSON<{ ack: { applied?: boolean; lap?: number } | null }>('/api/cut_lap/ack'),
    5000, [sent ?? '']);

  return (
    <Sec icon="timer" title="Cut lap">
      <button className="btn block" onClick={async () => {
        try {
          setSent((await postJSON<{ sentAt: string }>('/api/cut_lap', {})).sentAt);
          toast('Cut Lap sent to the car');
        } catch (e) { toast(`Cut Lap failed: ${e}`, 'err'); }
      }}><Icon name="flag" size={13} />Cut lap now</button>
      <div className="caption">
        {sent
          ? (ack?.ack?.applied
              ? `Car confirmed — now on lap ${ack.ack.lap} · sent ${sent}`
              : `Sent ${sent} — awaiting the car's confirmation.`)
          : 'Asks the car to close its lap. Does not change the manual override.'}
      </div>
    </Sec>
  );
}

function DriverMessage() {
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
    </Sec>
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
      const res = await fetch(url);
      if (!res.ok) throw new Error(`server said ${res.status}`);
      const rows = Number(res.headers.get('X-Row-Count') || 0);
      const cd = res.headers.get('content-disposition') || '';
      const name = /filename="([^"]+)"/.exec(cd)?.[1] ?? 'telemetry.xlsx';
      const blob = await res.blob();
      const href = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = href;
      link.download = name;
      link.click();
      URL.revokeObjectURL(href);
      setDone(`${name} · ${(blob.size / 1e6).toFixed(1)} MB · ${rows.toLocaleString()} rows in ${((Date.now() - t0) / 1000).toFixed(0)} s`);
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
      <input type="datetime-local" value={a} disabled={busy} onChange={(e) => setFrom(e.target.value)} />
      <label className="fld">To</label>
      <input type="datetime-local" value={b} disabled={busy} onChange={(e) => setTo(e.target.value)} />
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
