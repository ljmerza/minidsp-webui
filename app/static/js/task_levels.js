// Set levels (phase 9 design §14.6): the wizard's live meters ("levelWatch", an Alpine component in
// templates/tasks/levels/_meters.html).
//
// It listens to the page's level feed (the hidden [data-clip-feed] element every page has; "levels" frames are
// {"i": [...], "o": [...]} in dBFS, ~4 Hz) and keeps, for one side, each channel's level, its peak since the step
// opened and how often it reached CLIP (a rising edge at -1 dBFS). The step's form sends the peaks and counts to the
// server as comma-separated hidden fields (pk / cl), which works the feedback out. ``secs``: watch that long, then
// press the step's [data-watch-done] button; ``wait``: after that many seconds without signal, say so.
(() => {
  'use strict';

  // Keep in sync with app/web/templating.py (meter_fraction) and static/js/meters.js.
  const FLOOR_DB = -120;
  const TICKS = [-120, -60, -40, -30, -20, -12, -6, 0];
  const CLIP_DBFS = -1;
  const MINUS = '−';

  function fraction(db) {
    if (db === null || db === undefined || !Number.isFinite(db)) return 0;
    const v = Math.min(0, Math.max(FLOOR_DB, db));
    const step = 1 / (TICKS.length - 1);
    for (let k = 0; k < TICKS.length - 1; k++) {
      if (v <= TICKS[k + 1]) return (k + (v - TICKS[k]) / (TICKS[k + 1] - TICKS[k])) * step;
    }
    return 1;
  }

  function dbfs(v) {
    if (v === null || v === undefined || !Number.isFinite(v) || v <= FLOOR_DB) return 'no signal';
    const text = Math.abs(v).toFixed(1);
    return `peak ${v < 0 && Number(text) !== 0 ? MINUS : ''}${text} dBFS`;
  }

  function levelWatch(cfg) {
    const n = (cfg.names || []).length;
    const hot = new Array(n).fill(false);
    let started = 0;
    let timer = null;
    let feed = null;
    let listener = null;
    return {
      names: cfg.names || [],
      skip: cfg.skip || [],
      level: new Array(n).fill(null),
      peak: new Array(n).fill(null),
      clips: new Array(n).fill(0),
      signal: false,
      noSignal: false,
      left: cfg.secs || 0,
      done: false,

      init() {
        started = Date.now();
        feed = document.querySelector('[data-clip-feed]');
        listener = (e) => this.onFrame(e.detail);
        if (feed) feed.addEventListener('sse:levels', listener);
        timer = setInterval(() => this.tick(), 250);
      },

      destroy() {
        clearInterval(timer);
        if (feed && listener) feed.removeEventListener('sse:levels', listener);
      },

      onFrame(event) {
        if (this.done || !event || typeof event.data !== 'string') return;
        let data;
        try {
          data = JSON.parse(event.data);
        } catch {
          return; // a malformed frame; the next one comes in ~250 ms
        }
        const values = data ? data[cfg.side] : null;
        if (!Array.isArray(values)) return;
        values.forEach((v, i) => {
          if (i >= n || typeof v !== 'number' || Number.isNaN(v)) return;
          this.level[i] = v;
          if (this.peak[i] === null || v > this.peak[i]) this.peak[i] = v;
          const isHot = v >= CLIP_DBFS;
          if (isHot && !hot[i]) this.clips[i] += 1;
          hot[i] = isHot;
          if (v > cfg.floor && !this.skip[i]) this.signal = true;
        });
      },

      tick() {
        const secs = (Date.now() - started) / 1000;
        this.noSignal = !this.signal && Boolean(cfg.wait) && secs >= cfg.wait;
        if (!cfg.secs || this.done) return;
        this.left = Math.max(0, Math.ceil(cfg.secs - secs));
        if (this.left === 0) {
          this.done = true;
          const button = this.$root.querySelector('[data-watch-done]');
          if (button && button.isConnected) button.click();
        }
      },

      width(i) {
        return `${(fraction(this.level[i]) * 100).toFixed(1)}%`;
      },

      peakText(i) {
        return dbfs(this.peak[i]);
      },

      get pk() {
        return this.peak.map((v) => (v === null ? '' : v.toFixed(1))).join(',');
      },

      get cl() {
        return this.clips.join(',');
      },
    };
  }

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('levelWatch', levelWatch);
  });

  window.mdsp = Object.assign(window.mdsp || {}, { levelWatch: { fraction, dbfs } });
})();
