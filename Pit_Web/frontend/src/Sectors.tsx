// Sector times — "is this lap better or worse than the last one, and where?"
//
// The F1 arrangement, because it is the one the crew already reads: a stable
// row for the lap just finished, and the lap in progress filling in beneath it
// sector by sector. Columns line up between the rows, so a single sector reads
// vertically and a driver losing time in one corner is visible without
// arithmetic.
//
// WHAT THIS FILE DOES NOT DO: decide anything. Every colour arrives from the
// server as `cls`, every time as `number | null`. The browser formats and
// nothing else — see api.py's _row(). That is the same rule that keeps the
// tier colours out of JavaScript.
//
// Before this, the grid showed only the lap IN PROGRESS, so most cells were
// dashes most of the time, and the one moment all nine were populated lasted
// about one four-second poll before the lap rolled and cleared them.

import { MISSING } from './lib';
import { Pill } from './components';
import type { Sectors, SplitCell, SplitRow } from './types';

function signed(n: number): string {
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}`;
}

/** One cell. The delta sits under the time in every state that has one, so
 *  colour is never the only thing carrying the message. */
function Cell({ cell }: { cell: SplitCell }) {
  const cls = ['scell', cell.state, cell.cls ?? ''].filter(Boolean).join(' ');
  return (
    <span className={cls}
          title={cell.state === 'missing'
            ? 'The car drove this sector but the telemetry did not arrive'
            : cell.state === 'pending' ? 'Not reached yet' : undefined}>
      <b>{cell.value === null ? MISSING : cell.value.toFixed(2)}</b>
      <i>{cell.delta === null ? ' ' : signed(cell.delta)}</i>
      {cell.cls === 'best' ? <em>best</em> : null}
    </span>
  );
}

function Row({ row }: { row: SplitRow }) {
  return (
    <div className={`splits-row ${row.kind}`}>
      <span className="lab">
        {row.label}
        {row.lap === null ? null : <small>lap {row.lap}</small>}
      </span>
      {row.cells.map((c) => <Cell key={c.sector} cell={c} />)}
      <span className={`scell lap ${row.total === null ? 'pending' : ''}`}>
        <b>{row.total === null ? MISSING : row.total.toFixed(2)}</b>
        <i>{row.totalDelta === null ? ' ' : signed(row.totalDelta)}</i>
      </span>
    </div>
  );
}

export function SectorTimes({ data }: { data: Sectors | null }) {
  if (!data) return <div className="caption">loading sector times…</div>;

  // The race gate is answered by the SERVER. The browser gets the race clock
  // over the websocket and the splits over HTTP, so deciding here would let
  // the two disagree for a tick.
  if (!data.rows.length) {
    return <Pill kind="info">{data.note}</Pill>;
  }

  return (
    <div className="splits" style={{ ['--cols' as string]: data.sectors.length }}>
      <div className="splits-row head">
        <span className="lab" />
        {data.sectors.map((s) => <span key={s} className="cell">S{s}</span>)}
        <span className="cell">LAP</span>
      </div>
      {data.rows.map((r) => <Row key={r.kind} row={r} />)}
      <div className="caption splits-key">
        <span className="key best">purple</span> best this race ·{' '}
        <span className="key faster">green</span> faster than last lap ·{' '}
        <span className="key slower">yellow</span> slower ·{' '}
        <span className="key missing">dashed</span> telemetry lost
      </div>
    </div>
  );
}
