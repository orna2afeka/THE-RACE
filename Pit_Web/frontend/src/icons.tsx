// Inline SVG icon set (Lucide-style outline paths, MIT).
//
// Inline, not an icon font or a CDN sprite: the pit laptop is frequently offline
// at the track, and `currentColor` lets every glyph inherit the text colour of
// whatever it sits in — a tier-coloured badge, a muted label, a button.

import type { CSSProperties } from 'react';

const PATHS: Record<string, string[]> = {
  zap: ['M13 2 3 14h9l-1 8 10-12h-9l1-8z'],
  activity: ['M22 12h-4l-3 9L9 3l-3 9H2'],
  gauge: ['M12 21a9 9 0 1 1 9-9', 'M12 12l4.5-4.5', 'M21 12h-2'],
  history: ['M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8', 'M3 3v5h5', 'M12 7v5l4 2'],
  sun: ['M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10z', 'M12 1v2', 'M12 21v2', 'M4.22 4.22l1.42 1.42',
        'M18.36 18.36l1.42 1.42', 'M1 12h2', 'M21 12h2', 'M4.22 19.78l1.42-1.42', 'M18.36 5.64l1.42-1.42'],
  moon: ['M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z'],
  cloud: ['M17.5 19a4.5 4.5 0 1 0-1.1-8.86A6 6 0 0 0 5 12.5 3.5 3.5 0 0 0 6.5 19h11z'],
  route: ['M6 22a3 3 0 1 0 0-6 3 3 0 0 0 0 6z', 'M18 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
          'M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15'],
  flag: ['M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z', 'M4 22v-7'],
  send: ['M22 2 11 13', 'M22 2l-7 20-4-9-9-4z'],
  download: ['M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'M7 10l5 5 5-5', 'M12 15V3'],
  panel: ['M3 5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z', 'M9 3v18'],
  alert: ['M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z',
          'M12 9v4', 'M12 17h.01'],
  check: ['M22 11.08V12a10 10 0 1 1-5.93-9.14', 'M22 4 12 14.01l-3-3'],
  pin: ['M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0z', 'M12 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6z'],
  timer: ['M10 2h4', 'M12 14l3-3', 'M12 22a8 8 0 1 0 0-16 8 8 0 0 0 0 16z'],
  radio: ['M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z', 'M16.24 7.76a6 6 0 0 1 0 8.49',
          'M7.76 16.24a6 6 0 0 1 0-8.49', 'M20.07 4.93a10 10 0 0 1 0 14.14', 'M3.93 19.07a10 10 0 0 1 0-14.14'],
  message: ['M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'],
  sliders: ['M4 21v-7', 'M4 10V3', 'M12 21v-9', 'M12 8V3', 'M20 21v-5', 'M20 12V3',
            'M1 14h6', 'M9 8h6', 'M17 16h6'],
  pause: ['M6 4h4v16H6z', 'M14 4h4v16h-4z'],
  play: ['M5 3l14 9-14 9V3z'],
  wifi: ['M5 12.55a11 11 0 0 1 14.08 0', 'M1.42 9a16 16 0 0 1 21.16 0', 'M8.53 16.11a6 6 0 0 1 6.95 0',
         'M12 20h.01'],
  wifioff: ['M1 1l22 22', 'M16.72 11.06A10.94 10.94 0 0 1 19 12.55', 'M5 12.55a10.94 10.94 0 0 1 5.17-2.39',
            'M10.71 5.05A16 16 0 0 1 22.58 9', 'M1.42 9a15.91 15.91 0 0 1 4.7-2.88', 'M8.53 16.11a6 6 0 0 1 6.95 0',
            'M12 20h.01'],
  table: ['M3 3h18v18H3z', 'M3 9h18', 'M3 15h18', 'M9 3v18'],
  bolt: ['M7 2v11h3v9l7-12h-4l4-8z'],
  chevron: ['M9 18l6-6-6-6'],
  battery: ['M1 6h16a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H1z', 'M23 13v-2', 'M5 10v4'],
  thermo: ['M14 14.76V3.5a2.5 2.5 0 0 0-5 0v11.26a4.5 4.5 0 1 0 5 0z'],
  trash: ['M3 6h18', 'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6', 'M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2'],
  target: ['M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z', 'M12 18a6 6 0 1 0 0-12 6 6 0 0 0 0 12z', 'M12 14a2 2 0 1 0 0-4 2 2 0 0 0 0 4z'],
};

export function Icon({ name, size = 16, style, className }: {
  name: keyof typeof PATHS | string;
  size?: number;
  style?: CSSProperties;
  className?: string;
}) {
  const d = PATHS[name] ?? [];
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor"
         strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden
         className={className} style={{ flex: 'none', ...style }}>
      {d.map((p, i) => <path key={i} d={p} />)}
    </svg>
  );
}
