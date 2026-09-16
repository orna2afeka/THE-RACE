// Driver Telemetry — track position, sector splits, live GPS map.

import { useEffect, useRef } from 'react';
import * as maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
// Bundled by Vite with its imports, because MapLibre's own lookup pointed at
// /assets/maplibre-gl-worker.mjs -- a file the build never emits (404 on every
// load). See the setWorkerUrl call below.
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';
import { SectionTitle, SectorCard } from '../components';
import { Icon } from '../icons';
import { getJSON, usePoll } from '../lib';
import type { Config, Live, Sectors } from '../types';
import { SectorTimes } from '../Sectors';

maplibregl.setWorkerUrl(maplibreWorkerUrl);

const MAP_ZOOM = 14;
/** Positions kept for the live breadcrumb trail (~4 minutes at 2 s). */
const TRAIL_MAX = 120;

/* Esri World Imagery — no API key, and an opaque dark layer under it so an
   offline pit gets a panel with a live dot rather than a void. */
const SATELLITE_STYLE: maplibregl.StyleSpecification = {
  version: 8,
  sources: {
    'esri-imagery': {
      type: 'raster',
      tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
      tileSize: 256, minzoom: 0, maxzoom: 19,
      attribution: 'Esri, Maxar, Earthstar Geographics',
    },
  },
  layers: [
    { id: 'ground', type: 'background', paint: { 'background-color': '#0c1624' } },
    { id: 'esri-imagery', type: 'raster', source: 'esri-imagery', paint: { 'raster-opacity': 1 } },
  ],
};

const lineFeature = (coords: [number, number][]) => ({
  type: 'FeatureCollection' as const,
  features: coords.length > 1
    ? [{ type: 'Feature' as const, properties: {}, geometry: { type: 'LineString' as const, coordinates: coords } }]
    : [],
});

function CarMap({ live, config }: { live: Live; config: Config }) {
  const holder = useRef<HTMLDivElement | null>(null);
  const map = useRef<maplibregl.Map | null>(null);
  const marker = useRef<maplibregl.Marker | null>(null);
  const followed = useRef(true);
  const loaded = useRef(false);
  const trail = useRef<[number, number][]>([]);

  useEffect(() => {
    if (!holder.current || map.current) return;
    const m = new maplibregl.Map({
      container: holder.current, style: SATELLITE_STYLE,
      center: [config.mapFallback.lon, config.mapFallback.lat], zoom: MAP_ZOOM,
      attributionControl: { compact: true },
    });
    m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
    m.on('dragstart', () => { followed.current = false; });
    m.on('load', () => {
      // A breadcrumb of where the car has just been.
      m.addSource('trail', { type: 'geojson', data: lineFeature([]) });
      m.addLayer({ id: 'trail-line', type: 'line', source: 'trail',
                   paint: { 'line-color': '#00e0b4', 'line-width': 3, 'line-opacity': 0.9 },
                   layout: { 'line-join': 'round', 'line-cap': 'round' } });
      loaded.current = true;
    });
    map.current = m;

    const el = document.createElement('div');
    el.className = 'car-dot';
    marker.current = new maplibregl.Marker({ element: el })
      .setLngLat([config.mapFallback.lon, config.mapFallback.lat]).addTo(m);
    return () => { m.remove(); map.current = null; loaded.current = false; };
  }, [config.mapFallback.lat, config.mapFallback.lon]);

  // Car position + breadcrumb, every tick.
  useEffect(() => {
    const m = map.current;
    if (!m || !marker.current) return;
    const { lat, lon, has_gps } = live.state;
    marker.current.setLngLat([lon, lat]);
    marker.current.getElement().classList.toggle('nofix', !has_gps);
    if (!has_gps) return;
    const last = trail.current[trail.current.length - 1];
    if (!last || last[0] !== lon || last[1] !== lat) {
      trail.current = [...trail.current, [lon, lat] as [number, number]].slice(-TRAIL_MAX);
      if (loaded.current) (m.getSource('trail') as maplibregl.GeoJSONSource | undefined)?.setData(lineFeature(trail.current));
    }
    if (followed.current) m.easeTo({ center: [lon, lat], duration: 600 });
  }, [live.state.lat, live.state.lon, live.state.has_gps]);

  const fix = live.state.has_gps;
  return (
    <div className="mapwrap">
      <div ref={holder} style={{ width: '100%', height: '100%' }} />
      <div className="map-overlay">
        <span className={`status ${fix ? 'live' : 'stale'}`}>
          <Icon name="pin" size={13} />
          {fix ? `${live.state.lat.toFixed(5)}, ${live.state.lon.toFixed(5)}` : 'No GPS fix — paddock fallback'}
        </span>
        <button className="btn" style={{ padding: '5px 10px', fontSize: 'calc(12px * var(--pit-font-scale))' }} onClick={() => {
          followed.current = true;
          if (map.current && fix) map.current.easeTo({ center: [live.state.lon, live.state.lat], zoom: MAP_ZOOM });
        }}><Icon name="target" size={12} />Follow</button>
      </div>
    </div>
  );
}

export default function Driver({ live, config }: { live: Live; config: Config }) {
  const { data: sectors } = usePoll(() => getJSON<Sectors>('/api/sectors'), 4000);
  return (
    <>
      <SectionTitle icon="gauge" title="Track position" />
      <SectorCard live={live} config={config} />
      <SectionTitle icon="timer" title="Sector times"
                    right={sectors?.racing ? 'delta vs the previous lap' : undefined} />
      <SectorTimes data={sectors} />
      <SectionTitle icon="pin" title="Live GPS map" right="Esri World Imagery · no API key · live trail in green" />
      <CarMap live={live} config={config} />
    </>
  );
}
