// Shared presentation pieces.

import type { ReactNode } from 'react';
import { Icon } from './icons';
import { MISSING, fmt, lapTime } from './lib';
import { Sparkline } from './Sparkline';
import type { Config, Live, LiveTile, Num, Tier } from './types';

/** One stat tile.
 *
 *  `tier` comes from limits.classify() in Python — never computed here, so the
 *  pit and the driver HUD cannot disagree about a breach. A tier is never
 *  colour alone: the value takes the tier colour AND a labelled badge appears,
 *  so it still reads for a colourblind engineer or on a washed-out screen. */
export function MetricTile({ title, value, unit, tier, large, note, missing, text, trend }: {
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
}) {
  const t = tier === 'warning' || tier === 'critical' ? tier : '';
  const isMissing = missing ?? value === MISSING;
  return (
    <div className={`tile ${t}${large ? ' large' : ''}${isMissing ? ' missing' : ''}${text ? ' text' : ''}`}>
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
  const shown = m.lapTime
    ? lapTime(m.value as Num)
    : m.text
      ? ((m.value as string) || MISSING)
      : fmt(m.value as Num, m.spec);
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
              from a profile the car confirmed. Same rule as has_gps versus the
              paddock fallback. */}
          <div className={`sector-profile ${live.activeProfile?.source ?? 'default'}`}>
            {live.activeProfile?.source === 'default'
              ? `assuming ${live.activeProfile?.key ?? '—'} · car has not reported`
              : `from ${live.activeProfile?.key}${live.activeProfile?.source === 'ack' ? ' · via radio' : ''}`}
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

export function Disclosure({ icon, title, count, open, children }: {
  icon: string; title: string; count?: number | string; open?: boolean; children: ReactNode;
}) {
  return (
    <details className="disc" open={open}>
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
