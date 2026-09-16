// "The race started at ..." — for the case the pit actually hits.
//
// The race begins at 12:00 and nobody reaches the laptop until 12:20. Pressing
// Start race says the race began at 12:20, and the error is NOT cosmetic:
// lapDelta is (laps done - laps expected) and expected comes from elapsed time,
// so twenty missing minutes make the car read about 5.7 laps better than it is.
// That is the number the strategist acts on, and the strategy matrix inherits
// the same error.
//
// The panel previews the resulting clock BEFORE committing. A mistyped hour is
// then visible as "has run 13 h 20 m" rather than discovered later from a lap
// delta nobody could explain.

import { useState } from 'react';
import { DateTimeField } from './components';
import { Icon } from './icons';
import { hms, localInput, postJSON } from './lib';
import { toast } from './toast';
import type { DriverStint, Live } from './types';

const RACE_LENGTH_S = 24 * 3600;

function runFor(seconds: number): string {
  const m = Math.round(seconds / 60);
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} m` : `${m} min`;
}

export function StartTimePanel({ race, stint, onDone }: {
  race: Live['race'] | undefined;
  stint?: DriverStint;
  onDone?: () => void;
}) {
  const running = !!race?.isRacing;
  // Does the driver countdown move with the start time? The SERVER decides —
  // this only reports its answer, so the caption cannot promise one thing
  // while the endpoint does another. Before the green flag there is no stint
  // yet and the start will create one, which is the same outcome.
  const stintMoves = running ? !!stint?.followsRace : true;
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);

  const openPanel = () => {
    // Prefill with the current start when correcting, otherwise now. Local
    // wall clock, not toISOString(): the input reads as local, so a UTC value
    // would be off by the offset before the user touched anything.
    setValue(localInput(race?.startTime ?? Date.now() / 1000));
    setOpen(true);
  };

  const epoch = value ? new Date(value).getTime() / 1000 : NaN;
  const valid = Number.isFinite(epoch);
  const now = Date.now() / 1000;
  const elapsed = valid ? now - epoch : 0;
  const future = valid && elapsed < -60;
  const overLong = valid && elapsed > RACE_LENGTH_S;
  const blocked = !valid || future || busy;

  const submit = async () => {
    setBusy(true);
    try {
      await postJSON('/api/race', { isRacing: true, startTime: epoch });
      toast(running
        ? `Start time corrected — the race has run ${runFor(elapsed)}` +
          (stintMoves ? ', and the driver countdown with it' : '')
        : `Race started from ${new Date(epoch).toLocaleTimeString()} — already ${runFor(elapsed)} in`);
      setOpen(false);
      onDone?.();
    } catch (e) { toast(`Could not set the start time: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  if (!open) {
    return (
      <button className="linkbtn" onClick={openPanel}>
        <Icon name="history" size={12} />
        {running ? 'Correct start time' : 'Race started earlier?'}
      </button>
    );
  }

  return (
    <div className="starttime">
      <label className="fld" style={{ marginTop: 0 }}>The race actually started at</label>
      <DateTimeField label="Start time" value={value} autoFocus onChange={setValue}
                     onKeyDown={(e) => e.key === 'Enter' && !blocked && submit()} />

      {/* The preview is the safety feature: commit only what you can see. */}
      {!valid ? (
        <div className="caption">Pick a date and time.</div>
      ) : future ? (
        <div className="pill err" style={{ marginTop: 8 }}>
          <Icon name="alert" size={14} style={{ marginTop: 2 }} />
          <span>That is in the future. A race cannot have started later than now.</span>
        </div>
      ) : (
        <>
          <div className={`pill ${overLong ? 'warn' : 'info'}`} style={{ marginTop: 8 }}>
            <Icon name={overLong ? 'alert' : 'timer'} size={14} style={{ marginTop: 2 }} />
            <span>
              Race would have run <b>{runFor(elapsed)}</b> · countdown{' '}
              <b>{hms(Math.max(0, RACE_LENGTH_S - elapsed))}</b>
              {overLong && ' — that is past the full 24 hours, so the countdown would read zero.'}
            </span>
          </div>
          <div className="caption" style={{ marginTop: 6 }}>
            {stintMoves ? (
              <>The driver countdown moves to this time too, so a change that is
              already due shows as due. Press <b>Driver changed</b> if there was a
              swap you missed.</>
            ) : (
              <>The driver countdown is not affected — stint {stint?.stint} began at
              its own driver change, not at the start of the race.</>
            )}
          </div>
        </>
      )}

      <div className="btnrow" style={{ marginTop: 10 }}>
        <button className="btn primary" disabled={blocked} onClick={submit}>
          <Icon name="play" size={13} />{running ? 'Correct' : 'Start from this time'}
        </button>
        <button className="btn" disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
      </div>
    </div>
  );
}
