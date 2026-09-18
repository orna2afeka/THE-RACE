// Shared types.
//
// Every reading is `number | null` and that is the whole reason this is
// TypeScript. A metric the CAN bus never reported is UNKNOWN, not zero: 0 °C
// reads as a cold motor, 0 V as a flat pack, 0 A as a coasting car, and
// averaging those zeros corrupted stint statistics. Nothing in this app may
// write `value ?? 0`.

export type Num = number | null;

/** A tier from limits.classify(). Computed in Python, never re-derived here. */
export type Tier = 'normal' | 'warning' | 'critical';

export interface Metric {
  key: string;
  label: string;
  unit: string;
  /** From Pit_Dashboard/metrics.py. Never retyped in this app. */
  color: string;
}

export interface Threshold {
  warn: Num;
  crit: Num;
  lowSide: boolean;
  fullScale: Num;
}

export interface Config {
  tierColours: Record<string, string | null>;
  tierColoursLight: Record<string, string | null>;
  tiers: { normal: Tier; warning: Tier; critical: Tier };
  thresholds: Record<string, Threshold>;
  metrics: Metric[];
  historyWindows: Record<string, number | null>;
  historyDefaultMetrics: string[];
  strategies: { key: string; label: string; lap_time_min: number; energy_wh: number }[];
  defaultStrategyKey: string;
  sections: {
    names: Record<string, string>;
    turnLabels: Record<string, string>;
    risk: Record<string, Tier>;
    colors: Record<string, string>;
    bounds: Record<string, [number, number]>;
  };
  trackLengthM: number;
  dataStaleAfterS: number;
  targetLapTimeMin: number;
  driverStint: { limitS: number; warnS: number; critS: number };
  exportGroups: string[];
  liveMetricCount: number;
  liveMetricsPerRow: number;
  mapFallback: { lat: number; lon: number };
}

export interface LiveState {
  soc: Num; voltage: Num; current: Num; rpm: Num; temp: Num;
  power_w: Num; batt_temp: Num; pack_voltage: Num; motor_current: Num;
  regen_energy: Num; target_speed_kmh: Num; soc_ctrl: Num; trip_m: Num;
  motor_temp: Num; motor_ohms: Num;
  motor_map: string | null; motor_map_raw: Num;
  last_lap_energy: Num; total_race_energy: Num; last_lap_time_s: Num;
  lap_distance_m: Num; lap_source: string | null;
  auto_lap: Num; odometer_km: Num;
  /** The car's own lap tags (gate-based tracker). All null from an older car.
      zone: where the car is NOW - 'track' | 'pit_lane' | 'box'.
      last_lap_kind: 'flying' | 'in' | 'out' | 'in_out' | 'start' | 'suspect';
      only flying laps feed averages and strategy. */
  zone: string | null; track_pos_m: Num; current_lap: Num;
  last_lap_kind: string | null; last_lap_flags: string | null;
  last_lap_stopped_s: Num;
  lat: number; lon: number;
  /** Deliberately separate from lat/lon: the map falls back to the Zolder
   *  paddock so it has somewhere to centre, and this says whether the pin is
   *  real. 0,0 is a real place in the Atlantic.
   *
   *  LIVE, not merely present: the car goes on serving its last known fix
   *  after the receiver loses lock, so a position can be well-formed and an
   *  hour old. Decided in Python against limits.GPS_LIVE_MAX_AGE_S. */
  has_gps: boolean;
  /** There is a position on the row at all, however old. has_gps && !this is
   *  impossible; !has_gps && this means "last seen here". */
  has_gps_point: boolean;
  /** Seconds since the car's last usable fix, straight from its GPSReader.
   *  null on a car whose build predates the field. */
  gps_age_s: Num;
  /** When that fix was taken, epoch seconds on the CAR's clock — the same
   *  clock the row's timestamp is in. null when it cannot be said. */
  gps_fix_ts: Num;
  speed_kmh: Num;
  bms_has_error: number; bms_error_code: number; bms_protections: string;
  mms_has_error: number; mms_error_code: number; mms_alerts: string;
}

export interface LiveTile {
  label: string;
  unit: string;
  spec: string;
  note: string | null;
  text: boolean;
  value: Num | string;
  tier: Tier;
}

/** Which speed profile the PIT selected, and whether anyone selected one.
 *  "pit" is the profile chosen in the Strategy section and sent to the car;
 *  "default" means nobody has chosen yet and the target speed is an
 *  assumption. The CAR's own report is not a source here — the Strategy
 *  section shows that separately, via /api/strategy/ack. */
export interface ActiveProfile {
  key: string;
  source: 'pit' | 'default';
}

/** Purple, green, yellow. Decided server-side in api.py's _row(); the browser
 *  never compares two sector times itself. */
export type SplitCls = 'best' | 'faster' | 'slower' | null;

export interface SplitCell {
  sector: number;
  value: Num;
  delta: Num;
  cls: SplitCls;
  /** "pending" = the car has not reached this gate yet. "missing" = it drove
   *  through and we lost the telemetry. Opposite meanings on a pit wall. */
  state: 'ok' | 'pending' | 'missing';
}

export interface SplitRow {
  kind: 'last' | 'current';
  label: string;
  lap: number | null;
  cells: SplitCell[];
  /** Null until all nine have landed: a partial sum is a wrong lap time. */
  total: Num;
  totalDelta: Num;
}

export interface Sectors {
  racing: boolean;
  sectors: number[];
  rows: SplitRow[];
  best: { sector: number; value: Num; lap: number | null }[];
  note: string | null;
}

/** The car's own report about itself, from the heartbeat. `ok` and
 *  `problems` are decided in Python (api.car_health); the badge only renders. */
export interface CarHealth {
  ok: boolean;
  problems: string[];
  piUptimeS: Num;
  canState: string | null;
  canSilentS: Num;
  canFrames: Num;
  gpsFix: Num;
  gpsDetail: string | null;
  gpsAgeS: Num;
  gpsFixTs: Num;
}

/** One strategy's simulation, exactly as the engine ran it. The chart draws
 *  `points` and marks `stops`; it derives nothing. */
export interface StrategyStop {
  number: number; afterLap: number; atMin: number;
  socBefore: number; socAfter: number; chargeMin: number; stopMin: number;
}
export interface StrategyTrace {
  label: string; laps: number; swaps: number; lapTimeMin: number;
  totalTimeMin: number; timeUsedMin: number; pitMin: number;
  capacityWh: number; startWh: number; finalWh: number;
  stops: StrategyStop[];
  points: { minute: number; wh: number; kind: 'start' | 'lap' | 'swap' | 'stop' | 'charge' | 'hold' }[];
}
export interface StrategyResp {
  rows: Record<string, string | number>[];
  /** Index-aligned with rows; null where a strategy could not plan. */
  traces: (StrategyTrace | null)[];
  floorWh: number; capacityWh: number; minStopMin: number; maxStops: number;
  chargingCurveIsMeasured: boolean;
  /** Label -> laps measured, for the rows whose energy the car actually paid for. */
  measured: Record<string, number>;
  minLapsForMeasured: number;
  timeLeftMin: number;
  assumedFullPack: boolean;
  missing: string[];
}

/** One cell on the Cell Voltages tab, classified server-side. */
export interface CellTileData { id: number; label: string; value: Num; tier: Tier; staleS: Num }
export interface CellsResp {
  fresh: boolean; age: Num;
  /** BMS A / BMS B NTC probes, live. `stale` is decided server-side. */
  probes: { pack: string; cells: (CellTileData & { stale: boolean })[] }[];
  temps: {
    configured: boolean; warn: number; crit: number;
    groups: { name: string; lo: number; hi: number; label: string; cells: CellTileData[] }[];
    unmapped: CellTileData[];
  };
  voltages: {
    stringCount: Num; required: number; valid: number; ok: boolean; missing: string;
    warn: number; crit: number; modules: CellTileData[]; extra: CellTileData[]; extraRange: string;
  };
}

/** Rule 3.5.6: one of the four extremes over the last 2 h. `note` is built
 *  server-side (cell label, clock time, how long before the newest sample). */
export interface ExtremeTile {
  key: string; title: string; unit: string; spec: string;
  value: Num; tier: Tier; cell: Num; ts: Num; note: string;
}
export interface CellExtremesResp {
  endTs: Num; coversS: number; windowS: number; refreshS: number;
  /** empty: no telemetry stored · none: no plausible cell reading in the window */
  state: 'empty' | 'none' | 'partial' | 'full';
  span: string | null; coverText: string | null; end: string | null;
  tiles: ExtremeTile[];
}

export interface TrackStatus {
  section: string;
  target_speed: number;
  next_feature: string;
  next_feature_desc: string;
  next_feature_speed: number;
  distance_to_next: number;
}

/** The driver-change countdown. Constants come from pit_config.py; the
 *  client ticks the display between pushes so the number moves every second. */
export interface DriverStint {
  startedAt: Num;
  stint: number;
  driver: string | null;
  elapsedS: Num;
  /** Negative once the change is overdue. */
  remainingS: Num;
  limitS: number;
  warnS: number;
  critS: number;
  tier: Tier;
  overdue: boolean;
  canUndo: boolean;
  previousStintS: Num;
  /** True while this stint is still the one the green flag auto-started, so
      correcting the race start time moves it too. False once a driver change
      has been logged: that stint began at the change, not at the start. */
  followsRace: boolean;
  /** Race-time seconds banked from previous running periods. */
  accumulatedS: number;
  /** When the current running period began, or null while the race is
   *  stopped — which is what makes the countdown hold instead of drain. */
  runningSince: Num;
  running: boolean;
  /** Does the public spectator page show this driver name (or no name)?
   *  False while the write is pending or failing; null in the demo, which
   *  never publishes. */
  publicSynced: boolean | null;
}

export interface Live {
  ts: number;
  age: Num;
  fresh: boolean;
  state: LiveState;
  faults: string[];
  health: CarHealth;
  race: {
    isRacing: boolean; startTime: Num; elapsedMin: number;
    hoursLeft: number; minsLeft: number; secsLeft: number;
    /** A race reset is still reversible — the Danger zone shows Undo. */
    canUndo: boolean;
  };
  activeLap: Num;
  lapDelta: Num;
  odometerKm: Num;
  /** Wh used since this lap's trigger, net of regen — the same basis as
   *  last_lap_energy, so the two tiles compare directly. Null until the car
   *  has reported both a lap and an energy total. */
  currentLapEnergy: Num;
  /** How far into the lap the pit's earliest sample sits. Near 0 the figure
   *  above covers the whole lap; a large value means the start of the lap was
   *  never received and it understates. */
  currentLapEnergyFromM: Num;
  lapDistanceM: number;
  sectorId: number;
  sectorName: string;
  track: TrackStatus;
  activeProfile: ActiveProfile;
  tiers: Record<string, Tier>;
  liveMetrics: { group: string; metrics: LiveTile[] }[];
  driverStint: DriverStint;
}

export interface HistoryResponse {
  t: (string | null)[];
  series: Record<string, Num[]>;
  cursor: Num;
  rangebreaks: { bounds: [string, string] }[];
  bounds: { lo: Num; hi: Num };
  total: number;
  count: number;
  /** How many samples the range really holds, before thinning for the draw. */
  sampled: number;
  downsampled: boolean;
  /** IANA zone the `t` strings are in (naive local wall-clock, by the export rule). */
  tz: string;
}

export interface StatRow {
  key: string; label: string; unit: string; color: string;
  min: Num; avg: Num; max: Num; now: Num;
  samples: number; missing: number;
}
