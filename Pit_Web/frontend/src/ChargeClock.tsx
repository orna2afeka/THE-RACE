// The charging clock: an hour on the charger at most, three charges a race.
//
// The hour is the crew's own ceiling and the three is regulation — a fourth
// charge classifies the car behind everyone who made three. Both used to live
// in somebody's head at two in the morning. The rules for what starts and
// stops this are the SERVER's (api.py, "The charging clock"): the pit owns the
// clock because a car switched off on the charger cannot say it is charging.
//
// Every number here is served. The limit, the amber threshold and the three
// all come from Python; this file only counts down from an instant.

import { useState } from 'react';
import { Icon } from './icons';
import { hms, postJSON, useAlignedServerNow } from './lib';
import { toast } from './toast';
import type { Live } from './types';

type Charge = Live['charge'];

function chargeNow(c: Charge | undefined, serverNowS: number) {
  if (!c || !c.active || c.startedAt === null) return null;
  const elapsed = Math.max(0, serverNowS - c.startedAt);
  const left = c.limitS - elapsed;
  return {
    elapsed, left,
    tier: left < 0 ? 'critical' : left <= c.warnLeftS ? 'warning' : '',
  };
}

const signed = (s: number) => (s < 0 ? '+' : '') + hms(Math.floor(Math.abs(s)));

/** In the top bar, and ONLY while a charge is running: a clock that is always
 *  there is a clock nobody reads when it matters. Counts DOWN to the hour, then
 *  up past it with a "+", pulsing — the same language as an overdue stint. */
export function ChargeClock({ charge, clockOffsetMs }: { charge: Charge | undefined; clockOffsetMs: number }) {
  const serverNowS = useAlignedServerNow(clockOffsetMs, charge?.active ? charge.startedAt : null);
  const n = chargeNow(charge, serverNowS);
  if (!n || !charge) return null;
  return (
    <>
      <span className="clock-sep" />
      <div className={`clock stint ${n.tier}${n.left < 0 ? ' overdue' : ''}`}
           title={`Charge ${charge.count} of ${charge.maxStops}. On the charger ${hms(Math.floor(n.elapsed))}; the crew's limit is ${hms(charge.limitS)}.`}>
        <span className="k">
          {n.left < 0 ? 'Charging · over the hour' : 'Charging · time left'} · {charge.count}/{charge.maxStops}
        </span>
        <span className="v">{signed(n.left)}</span>
      </div>
    </>
  );
}

/** The sidebar half: the buttons, the count, and the last charge's length. */
export function ChargePanel({ charge, clockOffsetMs }: { charge: Charge | undefined; clockOffsetMs: number }) {
  const [busy, setBusy] = useState(false);
  const [ago, setAgo] = useState('');
  const serverNowS = useAlignedServerNow(clockOffsetMs, charge?.active ? charge.startedAt : null);
  if (!charge) return null;
  const n = chargeNow(charge, serverNowS);
  const usedUp = charge.count >= charge.maxStops;

  const act = async (action: 'start' | 'stop' | 'discard' | 'set_count', done: string,
                     extra: Record<string, number> = {}) => {
    setBusy(true);
    try {
      await postJSON('/api/charge', { action, ...extra });
      toast(done);
    } catch (e) { toast(`Charging clock: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  if (n) {
    return (
      <>
        <div className={`stint-readout ${n.tier}`}>
          <div className="k">{n.left < 0 ? 'Over the hour by' : 'Time left on the charger'}</div>
          <div className="v">{signed(n.left).replace('+', '')}</div>
          <div className="caption" style={{ margin: 0 }}>
            Charge {charge.count} of {charge.maxStops} · on for {hms(Math.floor(n.elapsed))} ·
            started by {charge.startedBy === 'car' ? 'the car' : 'the pit'}
          </div>
        </div>
        <button className="btn primary block" style={{ marginTop: 8 }} disabled={busy}
                onClick={() => act('stop', 'Charge ended — clock stopped')}>
          <Icon name="pause" size={13} />Charging ended
        </button>
        <button className="linkbtn" style={{ marginTop: 6 }} disabled={busy}
                onClick={() => act('discard', `Discarded — back to ${charge.count - 1} of ${charge.maxStops} charges`)}>
          That was not a charge — discard it
        </button>
        <div className="caption">
          Keeps counting if the car goes silent. Stops by itself when the car is heard moving.
        </div>
      </>
    );
  }

  return (
    <>
      <div className="caption" style={{ marginTop: 0 }}>
        {charge.count} of {charge.maxStops} charges used
        {charge.lastDurationS != null ? ` · last one ${hms(Math.floor(charge.lastDurationS))}` : ''}.
        {' '}Starts by itself when the car reports charging; press if the car is switched off.
      </div>
      {usedUp && (
        <div className="pill warn" style={{ marginTop: 8 }}>
          <Icon name="alert" size={14} style={{ marginTop: 2 }} />
          <span>All {charge.maxStops} charges are used. A fourth classifies the car behind every car that made {charge.maxStops} or fewer.</span>
        </div>
      )}
      {/* The press often comes late — the plug goes in, THEN someone reaches
          the laptop. Minutes-ago backdates the clock so the hour is measured
          from the plug and not from the press. Empty means now. */}
      <div className="btnrow" style={{ marginTop: 8 }}>
        <button className="btn" disabled={busy || (ago.trim() !== '' && !(Number(ago) >= 0))}
                onClick={() => {
                  const m = ago.trim() === '' ? 0 : Number(ago);
                  act('start', `Charging clock started${m ? ` from ${m} min ago` : ''} — charge ${charge.count + 1} of ${charge.maxStops}`,
                      { minutesAgo: m }).then(() => setAgo(''));
                }}>
          <Icon name="play" size={13} />Charging started
        </button>
        <input type="text" inputMode="numeric" aria-label="Minutes ago" placeholder="min ago"
               value={ago} onChange={(e) => setAgo(e.target.value)} style={{ width: 76 }} disabled={busy} />
      </div>
      {/* The count is regulation, and the store cannot always see a charge (a
          car switched off says nothing), so the pit's word is final. */}
      <div className="btnrow" style={{ marginTop: 8 }}>
        <span className="caption" style={{ margin: 0 }}>Charges used</span>
        <button className="btn" disabled={busy || charge.count <= 0}
                onClick={() => act('set_count', `Charges used: ${charge.count - 1}`, { count: charge.count - 1 })}>−</button>
        <b className="mono">{charge.count}</b>
        <button className="btn" disabled={busy}
                onClick={() => act('set_count', `Charges used: ${charge.count + 1}`, { count: charge.count + 1 })}>+</button>
      </div>
    </>
  );
}
