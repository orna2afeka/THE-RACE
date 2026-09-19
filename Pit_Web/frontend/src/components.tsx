// Shared presentation pieces.

import { useRef, type KeyboardEvent, type ReactNode } from 'react';
import { Icon } from './icons';
import { MISSING, fmt } from './lib';
import { Sparkline } from './Sparkline';
import type { Config, Live, LiveTile, Num, Tier } from './types';

/** One stat tile.
 *
 *  `tier` comes from limits.classify() in Python — never computed here, so the
 *  pit and the driver HUD cannot disagree about a breach. A tier is never
 *  colour alone: the value takes the tier colour AND a labelled badge appears,
 *  so it still reads for a colourblind engineer or on a washed-out screen. */
export function MetricTile({ title, value, unit, tier, large, note, missing, text, trend, stale }: {
  title: string;
  value: string;
  unit?: string;
  tier?: Tier;
  large?: boolean;
  note?: string | null;
  missing?: boolean;
  /** The value is a word (a power map name, a lap source), not a number. */
  text?: boolean;
  /** Recent values for a sparkline; nulls break the trace. */
  trend?: Num[];
  /** A carried-forward value past the stale limit. Marks the tile, never
   *  its tier colour: an old critical reading stays alarming. */
  stale?: boolean;
}) {
  const t = tier === 'warning' || tier === 'critical' ? tier : '';
  const isMissing = missing ?? value === MISSING;
  return (
    <div className={`tile ${t}${large ? ' large' : ''}${isMissing ? ' missing' : ''}${text ? ' text' : ''}${stale ? ' stale-carried' : ''}`}>
      <div className="tile-label">{title}</div>
      {t && (
        <span className={`tier-badge ${t}`}>
          <Icon name="alert" size={11} />{t === 'warning' ? 'warn' : 'crit'}
        </span>
      )}
      <div className="tile-value">
        <span>{value}</span>
        {unit ? <span className="tile-unit">{unit}</span> : null}
      </div>
      {note ? <div className="tile-note">{note}</div> : null}
      {trend && trend.length > 1 ? <div className="tile-spark"><Sparkline values={trend} accent /></div> : null}
    </div>
  );
}

/** A Live Metrics tile straight from the served catalogue. */
export function CatalogueTile({ m }: { m: LiveTile }) {
  const shown = m.text ? ((m.value as string) || MISSING) : fmt(m.value as Num, m.spec);
  return <MetricTile title={m.label} value={shown} unit={m.unit} tier={m.tier} large note={m.note} text={m.text} />;
}

export function FaultBanner({ live }: { live: Live }) {
  if (live.faults.length) {
    return (
      <div className={`banner ${live.fresh ? 'fault' : 'fault-stale'}`} role="alert">
        <Icon name="alert" size={18} />
        <span>ACTIVE FAULTS{live.fresh ? '' : ' · STALE'}</span>
        <span className="list">{live.faults.join('   |   ')}</span>
      </div>
    );
  }
  if (live.fresh) {
    return <div className="banner ok"><Icon name="check" size={16} />No active faults</div>;
  }
  return null;
}

/** Active power map. A badge rather than an eighth tile: strategy needs to see
 *  which energy configuration the car is on at a glance, and it is a state,
 *  not a measurement. Amber for reverse, which should never be live on track. */
export function PowerMapBadge({ state }: { state: Live['state'] }) {
  const map = state.motor_map;
  const kind = !map ? 'none' : /reverse/i.test(map) ? 'reverse' : 'normal';
  return (
    <div className={`powermap ${kind}`}>
      <Icon name="zap" size={13} style={{ color: 'var(--ink-3)' }} />
      <span className="k">Power map</span>
      {map ? (
        <>
          <span className="v">{map}</span>
          {state.motor_map_raw !== null
            ? <span className="raw">raw {Math.round(state.motor_map_raw)}</span>
            : null}
        </>
      ) : <span className="v">not reported</span>}
    </div>
  );
}

/** CHARGING, when the car says a charger is on it (charge_detector.py).
 *
 *  Renders NOTHING unless is_charging is exactly 1. Two reasons it is not a
 *  truthiness test: 0 is "not charging" and null is "this car's build cannot
 *  say", and neither may light the badge — but more importantly a badge that
 *  latched on and stayed on would be read, correctly, as the car still being
 *  in the box. Absent is the right rendering for both.
 *
 *  Deliberately not a tile: a tile reading "no" in the strip for 23 of 24
 *  hours is noise, and the one thing worth knowing here is the exception. */
export function ChargingBadge({ state }: { state: Live['state'] }) {
  if (state.is_charging !== 1) return null;
  return (
    <div className="charging">
      <Icon name="battery" size={13} />
      <span className="k">Charging</span>
      <span className="v">car stopped, current into the pack</span>
    </div>
  );
}

/** The sector card — a port of ui.render_sector_display(). */
export function SectorCard({ live, config }: { live: Live; config: Config }) {
  const sid = live.sectorId;
  const risk = (config.sections.risk[String(sid)] ?? 'normal') as Tier;
  const colour = config.sections.colors[risk];
  const currentClass = risk === 'normal' ? 'current' : `current-${risk}`;
  const name = config.sections.names[String(sid)] ?? `Section ${sid}`;
  const t = live.track;

  return (
    <div className="sector-card">
      <div className="sector-top">
        <div>
          <div className="sector-sub">Current sector</div>
          <div className="sector-headline" style={{ color: colour }}>SECTOR {sid}</div>
          <div className="sector-name">{name}</div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <div className="sector-sub">Target speed</div>
          <div className="sector-target" style={{ color: colour }}>
            {t.target_speed.toFixed(0)}<small>km/h</small>
          </div>
          <div className="sector-dist">{live.lapDistanceM.toFixed(0)} m / {config.trackLengthM} m</div>
          {/* A target speed from an ASSUMED profile must never look like one
              the pit actually chose. Same rule as has_gps versus the paddock
              fallback. */}
          <div className={`sector-profile ${live.activeProfile?.source ?? 'default'}`}>
            {live.activeProfile?.source === 'default'
              ? `assuming ${live.activeProfile?.key ?? '—'} · no profile sent yet`
              : `from ${live.activeProfile?.key} · pit selection`}
          </div>
        </div>
      </div>
      <div className="sector-strip">
        {Object.keys(config.sections.bounds).map(Number).sort((a, b) => a - b).map((i) => {
          const cls = i < sid ? 'past' : i === sid ? currentClass : 'future';
          return (
            <div key={i} className={`sector-seg ${cls}`}>
              <div>S{i}</div>
              <small>{i === sid ? (config.sections.turnLabels[String(i)] ?? '') : ''}</small>
            </div>
          );
        })}
      </div>
      <div className="next-feature">
        <div className="sector-sub">Next feature</div>
        <div className="row">
          <span className="name"><Icon name="chevron" size={14} />{t.next_feature}</span>
          <span className="dist">in <b>{t.distance_to_next.toFixed(0)} m</b></span>
          <span className="speed"><Icon name="bolt" size={11} />{t.next_feature_speed} km/h</span>
        </div>
        <div className="desc">“{t.next_feature_desc}”</div>
      </div>
    </div>
  );
}

/** Wraps controls that COMMAND THE CAR and switches them off where this
 *  dashboard has no business reaching it — a demo store, or a backend opened
 *  on an archived copy (config.demoStore).
 *
 *  A real disabled <fieldset>, so the browser disables everything inside it
 *  rather than each button remembering to check. The server refuses these
 *  commands as well (api.car_link); this is so nobody presses a button that
 *  was only ever going to fail, and so the demo still SHOWS the controls
 *  instead of hiding what the real dashboard looks like.
 */
export function CarControls({ enabled, note, children }:
  { enabled: boolean; note?: string; children: ReactNode }) {
  return (
    <>
      <fieldset className="nocar" disabled={!enabled}>{children}</fieldset>
      {!enabled && (
        <div className="caption">
          <b>Not connected to the car.</b> {note ?? 'This dashboard is on a demo store, so nothing here can command the car or reach the spectator page. Use the real pit dashboard for that.'}
        </div>
      )}
    </>
  );
}

export function Pill({ kind, children }: { kind: 'ok' | 'warn' | 'err' | 'info'; children: ReactNode }) {
  const icon = kind === 'ok' ? 'check' : kind === 'info' ? 'radio' : 'alert';
  return <div className={`pill ${kind}`}><Icon name={icon} size={14} style={{ marginTop: 2 }} /><span>{children}</span></div>;
}

export function SectionTitle({ icon, title, right }: { icon: string; title: string; right?: ReactNode }) {
  return (
    <div className="sec-title">
      <h3><Icon name={icon} size={16} style={{ color: 'var(--ink-3)' }} />{title}</h3>
      {right ? <span className="muted">{right}</span> : null}
    </div>
  );
}

/** `onToggle` reports open/closed, so a panel can put off work until it is
 *  actually looked at — the per-lap export uses it to defer a lap count that
 *  groups the whole telemetry table. Optional; every other caller ignores it. */
export function Disclosure({ icon, title, count, open, onToggle, children }: {
  icon: string; title: string; count?: number | string; open?: boolean;
  onToggle?: (open: boolean) => void; children: ReactNode;
}) {
  return (
    <details className="disc" open={open}
             onToggle={(e) => onToggle?.((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>
        <Icon name={icon} size={15} style={{ color: 'var(--ink-3)' }} />
        {title}
        {count !== undefined ? <span className="count">{count}</span> : null}
        <Icon name="chevron" size={14} className="chev" />
      </summary>
      <div className="disc-body">{children}</div>
    </details>
  );
}

/** A datetime-local input with a calendar button that opens the browser's own
 *  date/time picker. The native indicator is a faint glyph that is easy to miss
 *  on the dark theme, so the button makes it obvious, and clicking anywhere in
 *  the field opens it too. Typing still works where showPicker is missing. */
export function DateTimeField({ label, value, min, max, disabled, autoFocus, onChange, onKeyDown }: {
  label: string; value: string; min?: string; max?: string; disabled?: boolean; autoFocus?: boolean;
  onChange: (v: string) => void; onKeyDown?: (e: KeyboardEvent<HTMLInputElement>) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const open = () => {
    try { ref.current?.showPicker(); } catch { ref.current?.focus(); }
  };
  return (
    <div className="dtfield">
      <input ref={ref} type="datetime-local" value={value} min={min} max={max}
             disabled={disabled} autoFocus={autoFocus} aria-label={label}
             onClick={open} onKeyDown={onKeyDown} onChange={(e) => onChange(e.target.value)} />
      <button type="button" className="btn dtbtn" disabled={disabled} onClick={open}
              title={`Pick ${label.toLowerCase()} date`} aria-label={`Open calendar for ${label}`}>
        <Icon name="calendar" size={14} />
      </button>
    </div>
  );
}
