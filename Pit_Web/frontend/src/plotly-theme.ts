// One Plotly look for every chart, so they read as one system.
//
// Recessive chrome: hairline solid gridlines one step off the surface, no
// zeroline, no plot border, the surface itself transparent so the card behind
// shows through. Lines are 2px with round joins. Text wears ink tokens, never a
// series colour. Hover is unified with a dark tooltip that matches the cards.

export interface Theme {
  ink1: string; ink2: string; ink3: string;
  grid: string; card: string; line: string;
}

export function theme(dark: boolean): Theme {
  return dark
    ? { ink1: '#e8edf3', ink2: '#a3aebb', ink3: '#6e7987',
        grid: 'rgba(255,255,255,0.07)', card: '#1a2030', line: 'rgba(255,255,255,0.16)' }
    : { ink1: '#0f172a', ink2: '#475569', ink3: '#64748b',
        grid: 'rgba(15,23,42,0.08)', card: '#ffffff', line: 'rgba(15,23,42,0.2)' };
}

export function layoutBase(dark: boolean, height: number) {
  const t = theme(dark);
  return {
    height,
    margin: { l: 56, r: 24, t: 10, b: 40 },
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(0,0,0,0)',
    font: { family: "'Inter Variable', Inter, system-ui, sans-serif", size: 12, color: t.ink2 },
    xaxis: {
      gridcolor: t.grid, zeroline: false, linecolor: t.grid, tickcolor: 'rgba(0,0,0,0)',
      tickfont: { size: 11, color: t.ink3 },
    },
    yaxis: {
      gridcolor: t.grid, zeroline: false, linecolor: 'rgba(0,0,0,0)', tickcolor: 'rgba(0,0,0,0)',
      tickfont: { size: 11, color: t.ink3, family: "'JetBrains Mono', monospace" },
      title: { font: { size: 11, color: t.ink3 } },
    },
    hovermode: 'x unified',
    hoverlabel: {
      bgcolor: t.card, bordercolor: t.line,
      font: { family: "'JetBrains Mono', monospace", size: 12, color: t.ink1 },
    },
    legend: {
      orientation: 'h', x: 0, y: 1.06, xanchor: 'left',
      font: { size: 11, color: t.ink2 }, bgcolor: 'rgba(0,0,0,0)',
      itemwidth: 30,
    },
    dragmode: 'zoom',
  };
}

export const config = {
  responsive: true,
  displaylogo: false,
  scrollZoom: true,
  modeBarButtonsToRemove: ['lasso2d', 'select2d', 'toggleSpikelines', 'autoScale2d'],
};
