// The pit's own line on the public page.
//
// docs/index.html can already say the two things the system KNOWS -- a driver
// change and the charger -- and nothing at all about the rest: a tyre change,
// a puncture, scrutineering, a repair. To the families reading it, a car that
// has stopped for any of those looks like a car that has broken.
//
// So: five words, typed here, in front of everyone with the URL within 15 s.
//
// THE QUICK BUTTONS ARE THE POINT. This gets used with the car in the box and
// the crew's hands full; one press has to be enough. The field is for the
// thing nobody predicted, which is most of them.
//
// IT DOES NOT EXPIRE, like the driver-change flag it sits beside. The page
// prints how long it has been up, and this panel does the same, so a line
// left on after the car went back out is visible rather than quietly wrong.

import { useState } from 'react';
import { Icon } from './icons';
import { ageText, getJSON, postJSON, usePoll } from './lib';
import { toast } from './toast';

type NoteState = {
  enabled: boolean;
  note: { text: string; since: number | null } | null;
  synced: boolean | null;
  maxLen: number;
};

/** One press each, for the stops that happen over and over. Not a menu of
 *  every reason a car can stop -- that list cannot be written in advance. */
const QUICK = ['Changing tyres', 'In the pits', 'Repairs in the pit'];

export function PublicNote({ clockOffsetMs }: { clockOffsetMs: number }) {
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [bump, setBump] = useState(0);
  const { data } = usePoll(() => getJSON<NoteState>('/api/public/note'), 5000, [bump]);

  if (!data) return null;
  if (!data.enabled) {
    return <div className="caption">The spectator page is not published from this dashboard (demo store).</div>;
  }

  const send = async (value: string) => {
    setBusy(true);
    try {
      await postJSON('/api/public/note', { text: value });
      toast(value
        ? `Public page: "${value}" — shown within 15 s`
        : 'Public page: the note is down');
      setText('');
      setBump((n) => n + 1);
    } catch (e) { toast(`Note not sent: ${e}`, 'err'); }
    finally { setBusy(false); }
  };

  const up = data.note;
  const nowS = (Date.now() + clockOffsetMs) / 1000;
  const typed = text.trim();

  return (
    <>
      {up ? (
        <>
          <div className="pill warn" style={{ marginTop: 0 }}>
            <Icon name="message" size={14} style={{ marginTop: 2 }} />
            <span>
              The public page is showing <b>{up.text}</b>
              {up.since != null ? `, up ${ageText(Math.max(0, nowS - up.since))}` : ''}.
              {data.synced === false ? ' Not published yet — retrying.' : ''}
            </span>
          </div>
          <button className="btn block" style={{ marginTop: 8 }} disabled={busy}
                  onClick={() => void send('')}>
            <Icon name="trash" size={13} />Take the note down
          </button>
        </>
      ) : (
        <div className="caption" style={{ marginTop: 0 }}>
          Nothing from the pit on the public page. One press, or type five words:
        </div>
      )}

      <div className="btnrow" style={{ marginTop: 8, flexWrap: 'wrap' }}>
        {QUICK.map((q) => (
          <button key={q} className="btn" disabled={busy || up?.text === q}
                  onClick={() => void send(q)}>{q}</button>
        ))}
      </div>

      <input type="text" style={{ marginTop: 8 }} maxLength={data.maxLen}
             aria-label="Note for the public page"
             placeholder={up ? 'Change the wording…' : 'e.g. Front tyre change'}
             value={text} disabled={busy}
             onChange={(e) => setText(e.target.value)}
             onKeyDown={(e) => { if (e.key === 'Enter' && typed) void send(typed); }} />
      <button className="btn block" style={{ marginTop: 8 }}
              disabled={busy || !typed || typed === up?.text}
              onClick={() => void send(typed)}>
        <Icon name="send" size={13} />{up ? 'Replace the note' : 'Show this on the public page'}
      </button>
      <div className="caption">
        Everyone with the page's address sees this, until you take it down.
        Re-sending the same words keeps the clock it already has.
      </div>
    </>
  );
}
