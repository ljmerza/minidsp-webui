// Level meters fed by SSE "levels" events ({"i":[...],"o":[...]} in dBFS, ~4 Hz). The htmx sse extension re-dispatches
// each event as a DOM event named "sse:levels" on the element that has hx-trigger="sse:levels"; the bars are updated
// through Alpine bindings rather than HTMX swaps.
(() => {
  'use strict';

  // Keep in sync with app/web/templating.py (meter_fraction).
  const FLOOR_DB = -120;
  const KNEE_DB = -40;
  const KNEE_FRACTION = 0.25;
  const PEAK_HOLD_MS = 1500;
  const CLIP_DBFS = -1;
  const STALE_MS = 2500;

  function meterFraction(db) {
    if (db === null || db === undefined || !Number.isFinite(db)) return 0;
    const v = Math.min(0, Math.max(FLOOR_DB, db));
    if (v >= KNEE_DB) return KNEE_FRACTION + ((1 - KNEE_FRACTION) * (v - KNEE_DB)) / -KNEE_DB;
    return (KNEE_FRACTION * (v - FLOOR_DB)) / (KNEE_DB - FLOOR_DB);
  }

  window.mdsp = Object.assign(window.mdsp || {}, { meterFraction });

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('levelMeters', (cfg) => {
      const channels = [
        ...cfg.inputs.map((name, idx) => ({ key: `i${idx}`, side: 'i', idx, name })),
        ...cfg.outputs.map((name, idx) => ({ key: `o${idx}`, side: 'o', idx, name })),
      ];
      const blank = (value) => Object.fromEntries(channels.map((c) => [c.key, value]));
      const peakAt = blank(0);
      let lastAt = 0;
      let timer = null;
      let listener = null;

      return {
        level: blank(null),
        peak: blank(null),
        clip: blank(false),
        stale: true,

        init() {
          if (cfg.initial) this.apply(cfg.initial);
          listener = (e) => this.onMessage(e.detail);
          this.$el.addEventListener('sse:levels', listener);
          timer = setInterval(() => this.tick(), 250);
        },

        destroy() {
          clearInterval(timer);
          if (listener) this.$el.removeEventListener('sse:levels', listener);
        },

        onMessage(event) {
          if (!event || typeof event.data !== 'string') return;
          try {
            this.apply(JSON.parse(event.data));
          } catch {
            // ignore a malformed frame; the next one arrives in ~250 ms
          }
        },

        apply(data) {
          const now = performance.now();
          let any = false;
          for (const c of channels) {
            const values = data[c.side];
            const v = Array.isArray(values) ? values[c.idx] : undefined;
            if (typeof v !== 'number' || Number.isNaN(v)) continue;
            any = true;
            this.level[c.key] = v;
            if (this.peak[c.key] === null || v >= this.peak[c.key] || now - peakAt[c.key] > PEAK_HOLD_MS) {
              this.peak[c.key] = v;
              peakAt[c.key] = now;
            }
            if (v >= CLIP_DBFS) this.clip[c.key] = true;
          }
          if (any) {
            lastAt = now;
            this.stale = false;
          }
        },

        tick() {
          const now = performance.now();
          if (!this.stale && now - lastAt > STALE_MS) {
            this.stale = true;
            for (const c of channels) {
              this.level[c.key] = null;
              this.peak[c.key] = null;
            }
          }
        },

        barStyle(key) {
          const fraction = this.stale ? 0 : meterFraction(this.level[key]);
          return `clip-path: inset(${((1 - fraction) * 100).toFixed(2)}% 0 0 0)`;
        },

        // A horizontal bar (Home's cards and meter strip): the same scale, filling from the left.
        hbarStyle(key) {
          const fraction = this.stale ? 0 : meterFraction(this.level[key]);
          return `clip-path: inset(0 ${((1 - fraction) * 100).toFixed(2)}% 0 0)`;
        },

        // aria-valuenow of a role="meter" (dBFS on the meter's −120…0 scale; the floor without data).
        meterNow(key) {
          const v = this.level[key];
          if (this.stale || v === null || !Number.isFinite(v)) return FLOOR_DB;
          return Math.round(Math.max(FLOOR_DB, Math.min(0, v)) * 10) / 10;
        },

        peakStyle(key) {
          return `bottom: ${(meterFraction(this.peak[key]) * 100).toFixed(2)}%`;
        },

        readout(key) {
          const v = this.level[key];
          return this.stale || v === null ? '—' : window.mdsp.fmtDb(v, 1, 'dBFS');
        },

        clipLabel(key) {
          const c = channels.find((ch) => ch.key === key);
          const name = c ? c.name : key;
          return this.clip[key] ? `${name} clipped, press to reset` : `${name} not clipping`;
        },

        resetClip(key) {
          this.clip[key] = false;
        },

        resetAllClips() {
          for (const c of channels) this.clip[c.key] = false;
        },

        anyClip() {
          return channels.some((c) => this.clip[c.key]);
        },
      };
    });
  });
})();
