// A stat-tile sparkline: the last ~10 minutes of one metric as a hairline
// trace in the de-emphasis ink, with the most recent point in the accent.
//
// Null-safe by construction: a missing reading BREAKS the line. Each run of
// real values becomes its own polyline, so a dropout reads as a gap and never
// as a dive to zero — the same rule the main chart follows with connectgaps.

import { useEffect, useState } from 'react';
import { getJSON } from './lib';
import type { HistoryResponse, Num } from './types';

export function Sparkline({ values, width = 120, height = 26, accent = false }: {
  values: Num[]; width?: number; height?: number; accent?: boolean;
}) {
  const clean = values.filter((v): v is number => v !== null && Number.isFinite(v));
  if (clean.length < 2) return <svg width={width} height={height} aria-hidden />;
  const lo = Math.min(...clean), hi = Math.max(...clean);
  const span = hi - lo || 1;
  const n = values.length;
  const x = (i: number) => (i / Math.max(1, n - 1)) * (width - 2) + 1;
  const y = (v: number) => height - 2 - ((v - lo) / span) * (height - 4);

  // Split into runs of consecutive non-null values.
  const runs: string[] = [];
  let cur: string[] = [];
  values.forEach((v, i) => {
    if (v === null || !Number.isFinite(v)) { if (cur.length > 1) runs.push(cur.join(' ')); cur = []; return; }
    cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  });
  if (cur.length > 1) runs.push(cur.join(' '));

  let last = n - 1;
  while (last >= 0 && (values[last] === null || !Number.isFinite(values[last] as number))) last--;

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden className="spark">
      {runs.map((pts, i) => (
        <polyline key={i} points={pts} fill="none" stroke="currentColor" strokeWidth={1.5}
                  strokeLinejoin="round" strokeLinecap="round" opacity={0.55} />
      ))}
      {last >= 0 && (
        <circle cx={x(last)} cy={y(values[last] as number)} r={2.6}
                fill={accent ? 'var(--accent)' : 'currentColor'} />
      )}
    </svg>
  );
}

/** Polls a short window for several metrics and hands back one series each.
 *  ~80 points over 10 minutes is plenty for a 120px trace, and the heavy-read
 *  cache on the server means every viewer shares the same fetch. */
export function useSparklines(keys: string[], minutes = 10, everyMs = 10000) {
  const [series, setSeries] = useState<Record<string, Num[]>>({});
  const joined = keys.join(',');
  useEffect(() => {
    let dead = false;
    const run = () => getJSON<HistoryResponse>(
      `/api/history?metrics=${encodeURIComponent(joined)}&minutes=${minutes}&max_points=100`)
      .then((h) => { if (!dead) setSeries(h.series); })
      .catch(() => { /* the tile just shows no trend */ });
    run();
    const id = window.setInterval(run, everyMs);
    return () => { dead = true; clearInterval(id); };
  }, [joined, minutes, everyMs]);
  return series;
}
