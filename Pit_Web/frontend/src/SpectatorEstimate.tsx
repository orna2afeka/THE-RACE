// The spectator page's estimate: "show the public where the car should be".
//
// While the car is out of contact the public page sits on a frozen marker,
// which reads as a stopped car to the family it is for. From here the pit can
// say otherwise: a lap number and a place on track, and the page walks the
// marker round the 4:40 profile from the instant of the press, marking every
// figure it produces as estimated.
//
// STARTED HERE AND ONLY HERE, ENDED BY THE CAR. The server clears it on the
// first fresh sample, so this panel never has to be remembered — but it can be
// stopped by hand, and it says how long it has been running so an estimate
// left on through a pit stop is visible rather than silent.
//
// The two fields have NO silent defaults. They are prefilled from the car's
// last word as a starting point, but the number that goes on a public page is
// the one on screen when Start is pressed.

import { useState } from 'react';
import { Icon } from './icons';
import { ageText, getJSON, postJSON, usePoll } from './lib';
import { toast } from './toast';

type EstimateState = {
  enabled: boolean;
  active: boolean;
  estimate: { startedAt: number; lap: number; distM: number } | null;
  synced: boolean | null;
  carAgeS: number | null;
  carFresh: boolean;
  prefill: { lap: number | null; distM: number | null };
  trackLengthM: number;
};

export function SpectatorEstimate({ clockOffsetMs }: { clockOffsetMs: number }) {
  const [lap, setLap] = useState('');
  const [dist, setDist] = useState('');
  const [busy, setBusy] = useState(false);
  const [bump, setBump] = useState(0);
  const { data } = usePoll(() => getJSON<EstimateState>('/api/public/estimate'), 5000, [bump]);

  if (!data) return null;
  if (!data.enabled) {
    return <div className="caption">The spectator page is not published from this dashboard (demo store).</div>;
  }

  const lapN = lap.trim() === '' ? data.prefill.lap : Number(lap);
  const distN = dist.trim() === '' ? data.prefill.distM : Number(dist);
  const bad = lapN === null || distN === null
    || !Number.isInteger(lapN) || lapN < 0
    || !Number.isFinite(distN) || distN < 0 || distN >= data.trackLengthM;

  const start = async () => {
    setBusy(true);
    try {
      await postJSON('/api/public/estimate', { lap: lapN, distM: distN });
      toast(`Spectator page: estimating from lap ${lapN}, ${Math.round(distN as number)} m — shown within 15 s`);
      setLap(''); setDist('');
      setBump((n) => n + 1);
    } catch (e) { toast(`Estimate not started: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  const stop = async () => {
    setBusy(true);
    try {
      await postJSON('/api/public/estimate/stop', {});
      toast('Spectator page: estimate stopped — back to where the car was last seen');
      setBump((n) => n + 1);
    } catch (e) { toast(`Could not stop the estimate: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  if (data.active && data.estimate) {
    const nowS = (Date.now() + clockOffsetMs) / 1000;
    return (
      <>
        <div className="pill warn" style={{ marginTop: 0 }}>
          <Icon name="radio" size={14} style={{ marginTop: 2 }} />
          <span>
            The public page is showing an <b>estimated</b> car: from lap {data.estimate.lap},{' '}
            {Math.round(data.estimate.distM)} m, running {ageText(Math.max(0, nowS - data.estimate.startedAt))}.
            {data.synced === false ? ' Not published yet — retrying.' : ''}
          </span>
        </div>
        <button className="btn block" style={{ marginTop: 8 }} disabled={busy} onClick={stop}>
          <Icon name="pause" size={13} />Stop estimating
        </button>
        <div className="caption">It ends by itself the moment the car is heard again.</div>
      </>
    );
  }

  return (
    <>
      <div className="caption" style={{ marginTop: 0 }}>
        {data.carFresh
          ? 'The car is live — the public page is showing its real position.'
          : `Car silent ${ageText(data.carAgeS)}. Show the public where it should be, at the 4:40 pace:`}
      </div>
      <div className="btnrow" style={{ marginTop: 8 }}>
        <input type="text" inputMode="numeric" aria-label="Lap"
               placeholder={data.prefill.lap === null ? 'lap' : `lap ${data.prefill.lap}`}
               value={lap} onChange={(e) => setLap(e.target.value)}
               style={{ width: 84 }} disabled={busy || data.carFresh} />
        <input type="text" inputMode="numeric" aria-label="Metres into the lap"
               placeholder={data.prefill.distM === null ? 'metres' : `${Math.round(data.prefill.distM)} m`}
               value={dist} onChange={(e) => setDist(e.target.value)}
               style={{ width: 84 }} disabled={busy || data.carFresh} />
      </div>
      <button className="btn block" style={{ marginTop: 8 }}
              disabled={busy || bad || data.carFresh} onClick={start}>
        <Icon name="play" size={13} />Start estimated position
      </button>
      {!data.carFresh && (
        <div className="caption">
          {bad
            ? `Give a lap number and a position between 0 and ${data.trackLengthM} m.`
            : `Will start from lap ${lapN}, ${Math.round(distN as number)} m. Marked as estimated on the page.`}
        </div>
      )}
    </>
  );
}
