// System page (docs 05 §6): an overlay of the selected un-muted outputs with their electrical sum, and the integration
// panel (phase difference of a pair of outputs over 20–100 Hz, |Δφ| > threshold shaded). Everything is computed by
// the server (GET /ui/devices/{serial}/presets/{p}/system); this file only asks for it and draws it.
//
// Needs graph.js (window.mdsp.graph) and the vendored uPlot. The pure helpers are exported as window.mdsp.system
// (and CommonJS) for the node tests.
(() => {
  'use strict';

  const RELOAD_DELAY_MS = 350;
  const REGION_FILL = 'rgba(255, 92, 92, 0.16)';
  const MINUS = '−';

  // --- pure helpers (no DOM) --------------------------------------------------------------------------------------

  const signed = (text) => text.replace(/^-/, MINUS);
  const fixed = (v, digits) => {
    const s = v.toFixed(digits);
    return Number(s) === 0 ? (0).toFixed(digits) : signed(s);
  };
  const fmtHz = (f) => `${f < 100 ? fixed(f, 1) : fixed(f, 0)} Hz`;

  // Split the wrapped phase into runs that don't cross ±180°, each as a full-length array with nulls outside the
  // run. uPlot joins a series across interior nulls and only clips the gap, which leaves a vertical stroke at the
  // wrap; separate series end cleanly.
  function wrapSegments(values, limit = 180) {
    const clean = Array.from(values, (v) => (v === null || v === undefined || !Number.isFinite(v) ? null : v));
    const segments = [];
    let current = null;
    for (let i = 0; i < clean.length; i++) {
      const v = clean[i];
      if (v === null) {
        current = null;
        continue;
      }
      if (current === null || Math.abs(v - clean[i - 1]) > limit) {
        current = new Array(clean.length).fill(null);
        segments.push(current);
      }
      current[i] = v;
    }
    return segments;
  }

  // The integration plot's traces (one per wrap segment): Δφ in phase mode, the level difference as the "dB" value.
  function integrationTraces(integ) {
    const label = `Δφ ${integ.label_a} − ${integ.label_b}`;
    return wrapSegments(integ.phase_diff_deg).map((phase, k) => ({
      id: `dphi${k}`,
      label,
      color: integ.color_a || '#f0b429',
      width: 2,
      response: { freqs: integ.freqs, phase_deg: phase, magnitude_db: integ.level_diff_db },
    }));
  }

  function regionText(region) {
    const parts = [`${fmtHz(region.f_lo)}–${fmtHz(region.f_hi)}`];
    parts.push(`|Δφ| up to ${fixed(Math.abs(region.peak_dphi_deg), 0)}° at ${fmtHz(region.peak_freq)}`);
    if (Number.isFinite(region.worst_deficit_db)) {
      const deficit = -region.worst_deficit_db;
      parts.push(deficit >= 60 ? 'the sum nearly cancels' : `the sum is up to ${fixed(deficit, 1)} dB below in-phase`);
    }
    return parts.join(', ');
  }

  function cursorText(integ, idx) {
    if (!integ || idx === null || idx === undefined || idx < 0 || idx >= integ.freqs.length) return '';
    const dphi = integ.phase_diff_deg[idx];
    const f = fmtHz(integ.freqs[idx]);
    if (!Number.isFinite(dphi)) return `${f}: no phase (an output is silent)`;
    const bits = [`${f}: Δφ ${fixed(dphi, 0)}°`];
    const level = integ.level_diff_db[idx];
    if (Number.isFinite(level)) bits.push(`${integ.label_a} ${fixed(level, 1)} dB vs ${integ.label_b}`);
    const deficit = integ.deficit_db[idx];
    if (Number.isFinite(deficit)) bits.push(`sum ${fixed(-deficit, 1)} dB below in-phase`);
    return bits.join(' · ');
  }

  // Query string of the data route.
  function queryFor({ selected, muted, view, inputs, pairA, pairB }) {
    const mutedSet = new Set(muted || []);
    const outs = [...selected].filter((n) => !mutedSet.has(n)).sort((a, b) => a - b);
    const params = new URLSearchParams();
    params.set('outputs', outs.length ? outs.join(',') : 'none');
    params.set('range', view);
    params.set('inputs', inputs);
    const a = pairA === '' || pairA === null ? null : Number(pairA);
    const b = pairB === '' || pairB === null ? null : Number(pairB);
    params.set('pair', a === null || b === null || a === b ? 'none' : `${a},${b}`);
    return params.toString();
  }

  // --- DOM ------------------------------------------------------------------------------------------------------------

  let IntegrationPlot = null;
  // A ResponsePlot that shades the cancellation regions and draws the ±threshold lines under the curve.
  function createIntegrationPlot(el, opts) {
    const graph = window.mdsp.graph;
    if (!IntegrationPlot) {
      IntegrationPlot = class extends graph.ResponsePlot {
        drawMinorGrid(u) {
          super.drawMinorGrid(u);
          const { ctx, bbox } = u;
          ctx.save();
          ctx.fillStyle = REGION_FILL;
          for (const r of this.regions || []) {
            const x0 = u.valToPos(Math.max(r.f_lo, u.scales.x.min), 'x', true);
            const x1 = u.valToPos(Math.min(r.f_hi, u.scales.x.max), 'x', true);
            if (x1 > x0) ctx.fillRect(x0, bbox.top, Math.max(1, x1 - x0), bbox.height);
          }
          if (this.threshold) {
            ctx.strokeStyle = cssVarOr('--bad', '#ff5c5c');
            ctx.globalAlpha = 0.6;
            ctx.setLineDash([5, 4]);
            ctx.lineWidth = 1;
            ctx.beginPath();
            for (const v of [this.threshold, -this.threshold]) {
              const y = Math.round(u.valToPos(v, 'y', true)) + 0.5;
              ctx.moveTo(bbox.left, y);
              ctx.lineTo(bbox.left + bbox.width, y);
            }
            ctx.stroke();
          }
          ctx.restore();
        }
      };
    }
    return new IntegrationPlot(el, opts);
  }

  function cssVarOr(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function remember(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(`mdsp.system.${key}`);
      window.localStorage.setItem(`mdsp.system.${key}`, String(value));
    } catch {
      /* storage disabled */
    }
    return null;
  }

  async function getJson(url) {
    let response;
    try {
      response = await fetch(url, { headers: { Accept: 'application/json' }, credentials: 'same-origin' });
    } catch {
      return { ok: false, status: 0, data: { detail: 'the web UI server is unreachable' } };
    }
    const data = await response.json().catch(() => null);
    return { ok: response.ok, status: response.status, data };
  }

  function parseEvent(e) {
    const raw = e && e.detail && e.detail.data;
    if (typeof raw !== 'string') return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  // cfg: { url, preset, outputs: [all indexes], muted: [...], pair: [a, b] | null, names, threshold, integrationRange }
  function systemView(cfg) {
    const ctl = { plot: null, iplot: null, timer: null, seq: 0, traces: [], integ: null };
    const pair = cfg.pair || [null, null];

    return {
      view: remember('view') || (window.mdsp.graph ? window.mdsp.graph.defaultView('sub') : 'sub'),
      mode: remember('mode') || 'magnitude',
      unwrap: remember('unwrap') === 'true',
      selected: [...cfg.outputs],
      muted: [...(cfg.muted || [])],
      inputs: '0',
      pairA: pair[0] === null ? '' : String(pair[0]),
      pairB: pair[1] === null ? '' : String(pair[1]),
      warnings: [],
      regions: [],
      integrationOn: false,
      integrationNote: '',
      cursor: '',
      error: null,
      loading: false,
      empty: false,

      init() {
        for (const topic of ['desired', 'sync']) this.$el.addEventListener(`sse:${topic}`, (e) => this.onEvent(e));
        this.$nextTick(() => {
          const graph = window.mdsp.graph;
          ctl.plot = graph.create(this.$refs.graph, { view: this.view, mode: this.mode, unwrap: this.unwrap, height: 300 });
          ctl.iplot = createIntegrationPlot(this.$refs.integration, {
            view: cfg.integrationRange,
            mode: 'phase',
            height: 220,
            readout: false,
            onCursor: (readout) => {
              const idx = readout ? graph.nearestIndex(ctl.integ ? ctl.integ.freqs : [], readout.freq) : null;
              this.cursor = cursorText(ctl.integ, idx);
            },
          });
          ctl.iplot.threshold = cfg.threshold;
          this.load();
        });
      },

      destroy() {
        if (ctl.plot) ctl.plot.destroy();
        if (ctl.iplot) ctl.iplot.destroy();
      },

      isMuted(n) {
        return this.muted.includes(n);
      },
      isSelected(n) {
        return this.selected.includes(n) && !this.isMuted(n);
      },
      toggle(n, on) {
        this.selected = on ? [...new Set([...this.selected, n])] : this.selected.filter((x) => x !== n);
        this.load();
      },

      url() {
        const u = new URL(cfg.url, window.location.href);
        return `${u.pathname}?${queryFor(this)}`;
      },

      async load() {
        const seq = ++ctl.seq;
        this.loading = true;
        const res = await getJson(this.url());
        if (seq !== ctl.seq) return; // a newer request is on its way
        this.loading = false;
        if (!res.ok) {
          this.error = (res.data && res.data.detail) || `Could not compute the response (HTTP ${res.status})`;
          return;
        }
        this.error = null;
        const data = res.data;
        this.muted = data.muted || [];
        ctl.traces = data.traces || [];
        this.empty = ctl.traces.length === 0;
        this.warnings = data.warnings || [];
        this.drawMain();
        this.showIntegration(data.integration);
      },

      drawMain() {
        if (ctl.plot) ctl.plot.setTraces(ctl.traces);
      },

      showIntegration(integ) {
        ctl.integ = integ;
        this.integrationOn = Boolean(integ);
        this.cursor = '';
        if (!integ) {
          this.regions = [];
          this.integrationNote = this.pairA !== '' && this.pairA === this.pairB ? 'Pick two different outputs.' : '';
          if (ctl.iplot) {
            ctl.iplot.regions = [];
            ctl.iplot.setTraces([]);
          }
          return;
        }
        this.integrationNote = '';
        this.regions = integ.regions.map((r) => ({ ...r, text: regionText(r) }));
        if (ctl.iplot) {
          ctl.iplot.regions = integ.regions;
          ctl.iplot.setTraces(integrationTraces(integ));
        }
      },

      onEvent(e) {
        const data = parseEvent(e);
        if (!data || (data.preset !== undefined && data.preset !== cfg.preset)) return;
        clearTimeout(ctl.timer);
        ctl.timer = setTimeout(() => this.load(), RELOAD_DELAY_MS);
      },

      setMode(mode) {
        this.mode = mode;
        remember('mode', mode);
        if (ctl.plot) ctl.plot.setMode(mode, { unwrap: this.unwrap });
      },
      setUnwrap() {
        remember('unwrap', this.unwrap);
        if (ctl.plot) ctl.plot.setUnwrap(this.unwrap);
      },
      setView(view) {
        this.view = view;
        remember('view', view);
        if (ctl.plot) ctl.plot.setView(view);
        this.load();
      },
      autoFit() {
        if (ctl.plot) ctl.plot.autoFit();
      },
      resetY() {
        if (ctl.plot) ctl.plot.resetY();
      },
    };
  }

  const api = Object.freeze({ wrapSegments, integrationTraces, regionText, cursorText, queryFor, systemView });

  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') {
    window.mdsp = Object.assign(window.mdsp || {}, { system: api });
    if (typeof document !== 'undefined') {
      document.addEventListener('alpine:init', () => window.Alpine.data('systemView', systemView));
    }
  }
})();
