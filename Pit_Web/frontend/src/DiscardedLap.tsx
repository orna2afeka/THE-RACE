// A lap thrown away by mistake, and the button that puts it back.
//
// "Restart lap, don't count it" sits directly under "Cut lap now", and at the
// line, with the car going past, the wrong one gets pressed: a whole driven
// lap discarded uncounted. The car publishes nothing for a lap it was told to
// forget, so the lap list could never show it -- but the samples say exactly
// what was thrown away, and the server reads that back (api_laps_discarded).
//
// SHOWS ONLY WHEN THERE IS SOMETHING TO DECIDE. No discarded lap in the last
// hour, nothing on screen. One found: what it was, and one press to restore
// it.
//
// ONCE RESTORED IT GOES QUIET. The lap is back in the list, the charts and
// the workbook, and that is the whole point of the press -- so the panel has
// nothing left to ask. It used to answer by keeping the notice up with "Undo
// -- take lap 76 back out" under it, which put a button that DELETES a real
// driven lap on the pit wall for the rest of the race, next to the count the
// pit had just corrected to match it. At Zolder that sat there for an hour.
// A restore is undone the way anything else here is: press Restart lap again,
// or ask for the lap back the next time it is discarded.
//
// THE RECORD AND THE COUNT ARE TWO FACTS. This puts the lap back in the list,
// the charts and the workbook. The number on the car is "Set car lap number",
// right below -- the answer says which number, and only when the car reads
// less than it should, so a count already corrected is not "fixed" twice.

import { useState } from 'react';
import { Icon } from './icons';
import { getJSON, lapTime, postJSON, usePoll } from './lib';
import { toast } from './toast';

type Candidate = {
  finishedTs: number; lap: number; lapTimeS: number | null; distanceM: number;
  energyWh: number | null; regenWh: number | null; restored: boolean; at: string;
};

export function DiscardedLap() {
  const [busy, setBusy] = useState(false);
  const [bump, setBump] = useState(0);
  const { data } = usePoll(
    () => getJSON<{ candidates: Candidate[] }>('/api/laps/discarded'), 10000, [bump]);

  // Only laps still waiting on a decision. A restored one is settled.
  const list = (data?.candidates ?? []).filter((c) => !c.restored);
  if (!list.length) return null;

  // The server still takes `undo` -- api_laps_restore is the one place a
  // restore is recorded and the only place it can be withdrawn, and a restore
  // filed by mistake has to be reachable. Nothing on this panel sends it.
  const act = async (c: Candidate) => {
    setBusy(true);
    try {
      const r = await postJSON<{ setCarLapTo: number | null }>(
        '/api/laps/restore', { finishedTs: c.finishedTs, undo: false });
      toast(`Lap ${c.lap} restored` + (r.setCarLapTo !== null
        ? ` — the car still counts one short: set its lap number to ${r.setCarLapTo}`
        : ' — the car’s count already includes it'));
      setBump((n) => n + 1);
    } catch (e) { toast(`Restore failed: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  return (
    <>
      {list.map((c) => (
        <div key={c.finishedTs} style={{ marginTop: 8 }}>
          <div className="pill warn" style={{ marginTop: 0 }}>
            <Icon name="alert" size={14} style={{ marginTop: 2 }} />
            <span>
              A whole lap was thrown away at <b>{c.at}</b>
              {' — '}
              {c.lapTimeS !== null ? lapTime(c.lapTimeS) : '—'} · {Math.round(c.distanceM)} m
              {c.energyWh !== null ? ` · ${c.energyWh.toFixed(1)} Wh` : ''}.
              {' Restart lap was pressed with a full lap behind it.'}
            </span>
          </div>
          <button className="btn block" style={{ marginTop: 6 }} disabled={busy}
                  onClick={() => void act(c)}>
            <Icon name="check" size={13} />
            {`Restore it as lap ${c.lap}`}
          </button>
        </div>
      ))}
    </>
  );
}
