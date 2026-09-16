// plotly.js-dist-min ships no type declarations. Only the calls this app makes
// are declared, deliberately narrowly — a wrong signature here is caught by
// tsc rather than at runtime in the pit.
//
// The dist-min build is bundled into the output, not fetched from a CDN: the
// pit laptop is frequently offline at the track.
declare module 'plotly.js-dist-min' {
  export interface PlotlyDiv extends HTMLDivElement {
    data?: unknown[];
    layout?: { xaxis?: { range?: unknown[]; autorange?: unknown } };
    on(event: string, handler: (e: unknown) => void): void;
    removeAllListeners?(event?: string): void;
  }

  export function newPlot(
    div: HTMLElement,
    data: unknown[],
    layout?: unknown,
    config?: unknown,
  ): Promise<PlotlyDiv>;

  /** Appends to existing traces WITHOUT remounting or relayouting. */
  export function extendTraces(
    div: HTMLElement,
    update: { x?: unknown[][]; y?: unknown[][] },
    traceIndices: number[],
  ): Promise<PlotlyDiv>;

  export function relayout(div: HTMLElement, update: unknown): Promise<PlotlyDiv>;

  export function purge(div: HTMLElement): void;

  export const Plots: {
    /** Re-measure the container. Plotly's `responsive` config only listens
     *  to window resize, so a container that changes width on its own (the
     *  sidebar collapsing, a hidden tab becoming visible) needs this. */
    resize(div: HTMLElement): Promise<void>;
  };
}
