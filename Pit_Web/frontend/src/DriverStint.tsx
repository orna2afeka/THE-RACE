// The driver-change countdown.
//
// Regulations cap how long one driver may stay in the car, and missing a change
// is a penalty — so this is on the app bar beside the race clock, not buried in
// a panel. It comes from the server (SQLite, like the race clock), so every
// laptop, tablet and phone on the pit LAN shows the same number.
//
// IT ONLY RUNS WHILE THE RACE DOES. Before the green flag and through any
// stoppage the countdown holds, and says so. Otherwise it drains through setup
// and shows a false OVERDUE before the race has started.
//
// WHY THE CLIENT DOES THE ARITHMETIC. The socket pushes every 2 s, and a clock
// that jumps in twos reads as broken, so the remaining time is recomputed every
// second from the banked total plus the current running period. The LIMIT and
// the two thresholds come from pit_config.py — Python still owns every
// constant; the browser only adds and compares.

import { useState } from 'react';
import { Icon } from './icons';
import { hms, hmsSigned, postJSON, useAlignedServerNow } from './lib';
import { toast } from './toast';
import type { DriverStint as Stint, Tier } from './types';

interface Now {
  remaining: number | null;
  elapsed: number | null;
  tier: Tier;
  running: boolean;
  started: boolean;
}

/** Remaining seconds and the tier it falls in, ticked locally.
 *  `runningSince` is null whenever the race clock is stopped, which is what
 *  makes the countdown hold rather than drain. */
export function stintNow(stint: Stint | undefined, serverNowS: number): Now {
  if (!stint?.startedAt) {
    return { remaining: null, elapsed: null, tier: 'normal', running: false, started: false };
  }
  const elapsed = stint.accumulatedS
    + (stint.runningSince ? Math.max(0, serverNowS - stint.runningSince) : 0);
  const remaining = stint.limitS - elapsed;
  const tier: Tier = remaining <= stint.critS ? 'critical'
    : remaining <= stint.warnS ? 'warning' : 'normal';
  return { remaining, elapsed, tier, running: !!stint.runningSince, started: true };
}

/** The instant this stint's displayed countdown rolls over, modulo a second.
 *  elapsed = accumulatedS + (now - runningSince), so it crosses a whole second
 *  when (now - (runningSince - accumulatedS)) does. Null while the stint is not
 *  running: the countdown holds and nothing rolls. */
export function stintEpoch(stint: Stint | undefined): number | null {
  if (!stint?.startedAt || !stint.runningSince) return null;
  return stint.runningSince - stint.accumulatedS;
}

function label(n: Now): string {
  if (!n.started) return 'Driver stint';
  if (!n.running) return n.elapsed && n.elapsed > 0 ? 'Holding · race stopped' : 'Starts with the race';
  return n.remaining !== null && n.remaining < 0 ? 'Change overdue by' : 'Driver change in';
}

/** The app-bar clock, beside the race clock. */
export function StintClock({ stint, clockOffsetMs }: { stint: Stint | undefined; clockOffsetMs: number }) {
  const serverNowS = useAlignedServerNow(clockOffsetMs, stintEpoch(stint));
  const n = stintNow(stint, serverNowS);
  const overdue = n.running && n.remaining !== null && n.remaining < 0;
  return (
    <div className={`clock stint ${n.tier}${overdue ? ' overdue' : ''}${n.started && !n.running ? ' held' : ''}`}>
      <span className="k">
        {n.started && !n.running ? <Icon name="pause" size={9} style={{ marginRight: 4 }} /> : null}
        {label(n)}
      </span>
      <span className="v">{n.remaining === null ? '--:--:--' : hmsSigned(n.remaining)}</span>
    </div>
  );
}

/** Full-width alert once the change is due. Same treatment as a fault banner,
 *  because it carries the same kind of consequence. Shown while the race is
 *  stopped too — a stoppage is the ideal moment to swap. */
export function StintBanner({ stint, clockOffsetMs }: { stint: Stint | undefined; clockOffsetMs: number }) {
  const serverNowS = useAlignedServerNow(clockOffsetMs, stintEpoch(stint));
  const n = stintNow(stint, serverNowS);
  if (n.remaining === null || n.tier === 'normal') return null;
  const overdue = n.remaining < 0;
  return (
    <div className={`banner ${overdue ? 'stint-over' : 'stint-due'}`} role="alert">
      <Icon name="timer" size={18} />
      <span>
        {overdue
          ? `DRIVER CHANGE OVERDUE BY ${hms(-n.remaining)}`
          : `DRIVER CHANGE DUE IN ${hms(n.remaining)}`}
        {!n.running && ' · HELD'}
      </span>
      <span className="list">
        {stint?.driver ? `${stint.driver} has driven ` : 'Current driver has driven '}
        {hms(n.elapsed ?? 0)} of race time · stint {stint?.stint}
        {!n.running && ' · a stoppage is a good moment to swap'}
      </span>
    </div>
  );
}

/** Sidebar control: who is in, how long left, and the one button pressed at a
 *  driver change. One click, no confirmation — this gets pressed during a pit
 *  stop with people shouting. The undo covers a mis-click. */
export function StintPanel({ stint, clockOffsetMs, racing }: {
  stint: Stint | undefined; clockOffsetMs: number; racing: boolean;
}) {
  const serverNowS = useAlignedServerNow(clockOffsetMs, stintEpoch(stint));
  const [next, setNext] = useState('');
  const [busy, setBusy] = useState(false);
  const n = stintNow(stint, serverNowS);
  const overdue = n.remaining !== null && n.remaining < 0;

  const logChange = async () => {
    setBusy(true);
    try {
      const r = await postJSON<Stint>('/api/driver_stint', { driver: next.trim() || null });
      const prev = r.previousStintS;
      // The length of the stint just ended goes in the toast, so a mis-click
      // ("previous stint ran 00:00:03") is obvious the instant it happens.
      toast(
        `Stint ${r.stint} started${r.driver ? ` · ${r.driver}` : ''}` +
        (prev ? ` · previous stint ran ${hms(prev)}` : '') +
        (r.runningSince ? '' : ' · holding until the race starts'),
      );
      setNext('');
    } catch (e) { toast(`Driver change failed: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  const undo = async () => {
    setBusy(true);
    try {
      const r = await postJSON<Stint>('/api/driver_stint/undo', {});
      toast(`Undone — back to stint ${r.stint}${r.driver ? ` · ${r.driver}` : ''}`, 'info');
    } catch (e) { toast(`Undo failed: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  return (
    <>
      <div className={`stint-readout ${n.tier}${overdue ? ' overdue' : ''}${n.started && !n.running ? ' held' : ''}`}>
        <div className="k">
          {n.started && !n.running ? <Icon name="pause" size={10} style={{ marginRight: 4 }} /> : null}
          {!n.started ? 'No stint logged' : !n.running ? 'Held · race stopped' : overdue ? 'Overdue by' : 'Time left'}
        </div>
        <div className="v">{n.remaining === null ? '--:--:--' : hmsSigned(n.remaining)}</div>
        <div className="sub">
          {n.started
            ? <>Stint {stint?.stint}{stint?.driver ? ` · ${stint.driver}` : ''} · driven {hms(n.elapsed ?? 0)} of race time</>
            : <>Starts automatically with the race, or press below when the first driver gets in.</>}
        </div>
      </div>

      <label className="fld">Driver getting in (optional)</label>
      <input type="text" value={next} placeholder="e.g. Noa" autoComplete="off"
             onChange={(e) => setNext(e.target.value)}
             onKeyDown={(e) => e.key === 'Enter' && !busy && logChange()} />
      <button className={`btn block ${overdue || n.tier === 'critical' ? 'danger-solid' : 'primary'}`}
              style={{ marginTop: 10 }} disabled={busy} onClick={logChange}>
        <Icon name="timer" size={13} />
        {n.started ? 'Driver changed — reset timer' : 'Start driver stint'}
      </button>

      {stint?.canUndo && (
        <button className="btn block" style={{ marginTop: 8 }} disabled={busy} onClick={undo}>
          <Icon name="history" size={13} />Undo last change
        </button>
      )}
      <div className="caption">
        Limit {hms(stint?.limitS ?? 0)} of RACE time. The countdown runs with the race clock and
        holds whenever it stops, so a stoppage never eats into a driver's stint.
        {!racing && n.started && ' Currently held — it resumes at the green flag.'}
      </div>
    </>
  );
}
