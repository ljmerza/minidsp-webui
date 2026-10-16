// Response graphs on uPlot (docs 05 "Graph details"): log-frequency x axis with 1-2-5 decade ticks, magnitude / phase /
// group-delay modes, a hover readout of f, dB and phase for every trace, and fixed per-channel colours.
//
//   const plot = window.mdsp.graph.create(el, { view: 'sub', mode: 'magnitude' });
//   plot.setTraces([{ id: 'o1', label: 'Shakers', color: '#f0b429', response }, ...]);
//
// A trace's `response` is the server's Response.to_dict() or a window.mdsp.dsp response (same keys: freqs,
// magnitude_db, phase_deg, phase_unwrapped_deg, group_delay_ms). Traces on other grids are resampled onto the first
// trace's. Optional per trace: width (px), dash ([on, off] px), bold (composite curve), visible.
//
// Handles (PEQ editors): plot.setHandles([{ id, freq, value, color, active }]) draws them in magnitude mode;
// handleAt(clientX, clientY) and toData(clientX, clientY) map pointer events back to bands and to (freq, dB).
(() => {
  'use strict';

  const VIEWS = { sub: [5, 300], full: [10, 20000] };
  const DEFAULT_VIEW = 'sub';
  const DEFAULT_DB_RANGE = [-24, 24];
  const DB_FIT_FLOOR = -120; // auto-fit ignores the −200 dB floor of muted / deep-notch points
  const STEPS = [1, 2, 5];

  // Keep in sync with app/web/ui_common.py (DEFAULT_COLORS, FALLBACK_COLOR); Settings may override per channel.
  const COLORS = {
    input: ['#4ea1ff', '#a78bfa'],
    output: ['#3ccf7a', '#f0b429', '#ff7a59', '#22c3c3'],
    fallback: '#8c96a3',
    sum: '#e3e7ec',
  };

  const MODES = {
    magnitude: { key: 'magnitude_db', label: 'Magnitude', unit: 'dB' },
    phase: { key: 'phase_deg', unwrappedKey: 'phase_unwrapped_deg', label: 'Phase', unit: '°' },
    group_delay: { key: 'group_delay_ms', label: 'Group delay', unit: 'ms' },
  };

  // --- pure helpers (no DOM) --------------------------------------------------------------------------------------

  function channelColor(kind, index, overrides) {
    const custom = overrides && (overrides[`${kind}.${index}`] ?? overrides[kind]?.[index]);
    if (custom) return custom;
    const palette = COLORS[kind] || [];
    return palette[index] || COLORS.fallback;
  }

  const MINUS = '−';
  const signed = (text) => text.replace(/^-/, MINUS);
  const fixed = (v, digits) => {
    const s = v.toFixed(digits);
    return Number(s) === 0 ? (0).toFixed(digits) : signed(s); // no "−0.0"
  };

  function fmtFreq(f) {
    if (!Number.isFinite(f)) return '—';
    if (f < 100) return `${f.toFixed(1)} Hz`;
    if (f < 1000) return `${Math.round(f)} Hz`;
    return `${(f / 1000).toFixed(f < 10000 ? 2 : 1)} kHz`;
  }

  const fmtDb = (v) => (Number.isFinite(v) ? `${fixed(v, 1)} dB` : '—');
  const fmtPhase = (v) => (Number.isFinite(v) ? `${fixed(v, 0)}°` : '—');
  const fmtMs = (v) => (Number.isFinite(v) ? `${fixed(v, 2)} ms` : '—');

  // Axis label for a tick: 5, 10, 20, 50, 100, 200, 500, 1k, 2k, 5k, 10k, 20k.
  function tickLabel(f) {
    if (f >= 1000) return `${Number((f / 1000).toPrecision(3))}k`;
    return String(Number(f.toPrecision(3)));
  }

  // 1-2-5 ticks per decade inside [min, max].
  function logSplits(min, max) {
    const out = [];
    if (!(min > 0 && max > min)) return out;
    for (let e = Math.floor(Math.log10(min)); e <= Math.ceil(Math.log10(max)); e++) {
      for (const s of STEPS) {
        const f = Number((s * 10 ** e).toPrecision(12));
        if (f >= min * (1 - 1e-9) && f <= max * (1 + 1e-9)) out.push(f);
      }
    }
    return out;
  }

  // The 3, 4, 6, 7, 8, 9 × 10^e lines between the labelled ticks (faint minor grid).
  function minorSplits(min, max) {
    const out = [];
    if (!(min > 0 && max > min)) return out;
    for (let e = Math.floor(Math.log10(min)); e <= Math.ceil(Math.log10(max)); e++) {
      for (const s of [3, 4, 6, 7, 8, 9]) {
        const f = s * 10 ** e;
        if (f > min && f < max) out.push(f);
      }
    }
    return out;
  }

  // Index of the grid point nearest to f on a log axis (freqs ascending).
  function nearestIndex(freqs, f) {
    const n = freqs.length;
    if (!n) return -1;
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (freqs[mid] <= f) lo = mid;
      else hi = mid;
    }
    return Math.abs(Math.log(f / freqs[lo])) <= Math.abs(Math.log(freqs[hi] / f)) ? lo : hi;
  }

  // Linear interpolation in log-frequency of `values` (on `freqs`) onto `grid`; null outside the source range.
  function resample(freqs, values, grid) {
    const out = new Array(grid.length);
    let j = 0;
    for (let i = 0; i < grid.length; i++) {
      const f = grid[i];
      if (f < freqs[0] * (1 - 1e-12) || f > freqs[freqs.length - 1] * (1 + 1e-12)) {
        out[i] = null;
        continue;
      }
      while (j < freqs.length - 2 && freqs[j + 1] < f) j++;
      const f0 = freqs[j];
      const f1 = freqs[Math.min(j + 1, freqs.length - 1)];
      const t = f1 === f0 ? 0 : Math.log(f / f0) / Math.log(f1 / f0);
      const v0 = values[j];
      const v1 = values[Math.min(j + 1, values.length - 1)];
      out[i] = v0 + (v1 - v0) * Math.min(1, Math.max(0, t));
    }
    return out;
  }

  const sameGrid = (a, b) =>
    a.length === b.length && Array.prototype.every.call(a, (f, i) => Math.abs(f - b[i]) <= 1e-9 * f);

  // The plotted values of a trace in a mode, on `grid`.
  function traceValues(trace, mode, { unwrap = false, grid = null } = {}) {
    const m = MODES[mode] || MODES.magnitude;
    const r = trace.response || {};
    const key = unwrap && m.unwrappedKey && r[m.unwrappedKey] ? m.unwrappedKey : m.key;
    const values = r[key] ? Array.from(r[key]) : [];
    if (!grid || !r.freqs || sameGrid(r.freqs, grid)) return values;
    return resample(r.freqs, values, grid);
  }

  // Readout rows at grid index `idx`: f plus dB, phase and group delay for every visible trace.
  function readoutRows(traces, grid, idx) {
    if (idx === null || idx === undefined || idx < 0 || idx >= grid.length) return null;
    const f = grid[idx];
    const at = (trace, key) => {
      const r = trace.response || {};
      if (!r[key]) return null;
      if (!r.freqs || sameGrid(r.freqs, grid)) return r[key][idx];
      return resample(r.freqs, r[key], [f])[0];
    };
    return {
      freq: f,
      rows: traces
        .filter((t) => t.visible !== false)
        .map((t) => ({
          id: t.id,
          label: t.label,
          color: t.color,
          db: at(t, 'magnitude_db'),
          phase: at(t, 'phase_deg'),
          group_delay: at(t, 'group_delay_ms'),
        })),
    };
  }

  const niceStep = (span, target) => {
    const raw = span / target;
    const p = 10 ** Math.floor(Math.log10(raw));
    for (const s of [1, 2, 2.5, 5, 10]) if (s * p >= raw) return s * p;
    return 10 * p;
  };

  // Y range fitting the visible traces inside the x range [fMin, fMax], padded and rounded outwards.
  function autoRange(traces, mode, grid, [fMin, fMax], { unwrap = false } = {}) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const t of traces) {
      if (t.visible === false) continue;
      const values = traceValues(t, mode, { unwrap, grid });
      for (let i = 0; i < grid.length; i++) {
        const v = values[i];
        if (grid[i] < fMin || grid[i] > fMax || v === null || !Number.isFinite(v)) continue;
        if (mode === 'magnitude' && v < DB_FIT_FLOOR) continue;
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    }
    if (!Number.isFinite(lo)) return mode === 'magnitude' ? [...DEFAULT_DB_RANGE] : defaultRange(mode, unwrap);
    const minSpan = { magnitude: 6, phase: 90, group_delay: 1 }[mode] ?? 1;
    if (hi - lo < minSpan) {
      const mid = (hi + lo) / 2;
      lo = mid - minSpan / 2;
      hi = mid + minSpan / 2;
    }
    const step = mode === 'magnitude' ? [3, 6, 12, 24].find((x) => x >= (hi - lo) / 8) || 48 : niceStep(hi - lo, 8);
    const pad = step / 4;
    return [Math.floor((lo - pad) / step) * step, Math.ceil((hi + pad) / step) * step];
  }

  function defaultRange(mode, unwrap = false) {
    if (mode === 'magnitude') return [...DEFAULT_DB_RANGE];
    if (mode === 'phase' && !unwrap) return [-180, 180];
    return null; // auto
  }

  // Y ticks: phase in 45° / 90° steps, everything else on a nice step.
  function ySplits(mode, min, max) {
    const span = max - min;
    let step;
    if (mode === 'phase') step = span <= 400 ? 45 : span <= 900 ? 90 : Math.ceil(span / 8 / 180) * 180;
    else if (mode === 'magnitude') step = span <= 12 ? 1 : span <= 30 ? 3 : span <= 60 ? 6 : span <= 120 ? 12 : 24;
    else step = niceStep(span, 6);
    const out = [];
    for (let v = Math.ceil(min / step - 1e-9) * step; v <= max + 1e-9; v += step) out.push(Number(v.toFixed(6)));
    return out;
  }

  const yLabel = (mode, v) => signed(String(Number(v.toPrecision(6)))) + (mode === 'phase' ? '°' : '');

  // --- the plot -----------------------------------------------------------------------------------------------

  // Defaults for the readout; wrapped in :where() so app.css overrides them without specificity fights.
  const DEFAULT_CSS = `
:where(.graph-readout) { display: flex; flex-wrap: wrap; gap: 0.25rem 1rem; align-items: baseline; min-height: 1.6rem;
  font-size: 0.85rem; font-variant-numeric: tabular-nums; }
:where(.graph-readout-hint) { color: var(--text-muted, #8c96a3); }
:where(.graph-readout-freq) { font-weight: 600; min-width: 5.5rem; }
:where(.graph-readout-rows) { display: contents; list-style: none; }
:where(.graph-readout-rows li) { display: inline-flex; gap: 0.4rem; align-items: center; }
:where(.graph-swatch) { width: 0.75rem; height: 0.2rem; border-radius: 1px; display: inline-block; }
:where(.graph-readout-label) { color: var(--text-muted, #8c96a3); }`;

  function injectDefaultCss() {
    if (typeof document === 'undefined' || document.getElementById('mdsp-graph-css')) return;
    const style = document.createElement('style');
    style.id = 'mdsp-graph-css';
    style.textContent = DEFAULT_CSS;
    document.head.prepend(style);
  }

  function cssVar(name, fallback) {
    if (typeof document === 'undefined' || typeof getComputedStyle === 'undefined') return fallback;
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function resolveView(view) {
    if (Array.isArray(view) && view.length === 2 && view[0] > 0 && view[1] > view[0]) return [view[0], view[1]];
    return [...(VIEWS[view] || VIEWS[DEFAULT_VIEW])];
  }

  class ResponsePlot {
    constructor(el, opts = {}) {
      const UPlot = window.uPlot;
      if (!UPlot) throw new Error('uPlot is not loaded (static/vendor/uplot/uPlot.iife.min.js)');
      this.UPlot = UPlot;
      this.el = el;
      this.mode = MODES[opts.mode] ? opts.mode : 'magnitude';
      this.unwrap = Boolean(opts.unwrap);
      this.xRange = resolveView(opts.view ?? opts.range ?? DEFAULT_VIEW);
      this.yRanges = {}; // mode -> [min, max] once auto-fitted or set
      this.height = opts.height || 260;
      this.traces = [];
      this.grid = [];
      this.handles = [];
      this.onCursor = opts.onCursor || null;
      this.title = opts.title || '';
      this.signature = null;
      this.plot = null;

      injectDefaultCss();
      el.classList.add('graph');
      this.canvasEl = document.createElement('div');
      this.canvasEl.className = 'graph-canvas';
      el.appendChild(this.canvasEl);
      if (opts.readout === false) {
        this.readoutEl = null;
      } else if (opts.readout && opts.readout.nodeType === 1) {
        this.readoutEl = opts.readout;
      } else {
        this.readoutEl = document.createElement('div');
        this.readoutEl.className = 'graph-readout';
        el.appendChild(this.readoutEl);
      }
      if (this.readoutEl) this.readoutEl.setAttribute('aria-live', 'off');
      this.renderReadout(null);

      this.colors = {
        axis: cssVar('--text-muted', '#8c96a3'),
        grid: cssVar('--border', '#2a313b'),
        text: cssVar('--text', '#e3e7ec'),
      };

      if (typeof ResizeObserver !== 'undefined') {
        this.resizeObserver = new ResizeObserver(() => this.resize());
        this.resizeObserver.observe(el);
      }
      this.build();
    }

    // --- public API ---

    setTraces(traces) {
      this.traces = traces.map((t, i) => ({ ...t, id: t.id ?? String(i), color: t.color || COLORS.fallback }));
      const first = this.traces.find((t) => t.response && t.response.freqs);
      this.grid = first ? Array.from(first.response.freqs) : [];
      const signature = JSON.stringify(
        this.traces.map((t) => [t.id, t.label, t.color, t.width, t.dash, t.bold, t.visible !== false]),
      );
      if (signature !== this.signature) {
        this.signature = signature;
        this.build();
      } else {
        this.plot.setData(this.data(), false);
        this.applyY();
      }
      this.refreshReadout();
    }

    setMode(mode, { unwrap } = {}) {
      if (!MODES[mode]) throw new Error(`unknown graph mode '${mode}'`);
      this.mode = mode;
      if (unwrap !== undefined) this.unwrap = Boolean(unwrap);
      this.build();
    }

    setUnwrap(unwrap) {
      this.unwrap = Boolean(unwrap);
      if (this.mode === 'phase') {
        delete this.yRanges.phase;
        this.build();
      }
    }

    setView(view) {
      this.xRange = resolveView(view);
      if (this.plot) this.plot.setScale('x', { min: this.xRange[0], max: this.xRange[1] });
    }

    setYRange(range) {
      if (range) this.yRanges[this.mode] = [range[0], range[1]];
      else delete this.yRanges[this.mode];
      this.applyY();
    }

    autoFit() {
      this.yRanges[this.mode] = autoRange(this.traces, this.mode, this.grid, this.xRange, { unwrap: this.unwrap });
      this.applyY();
      return this.yRanges[this.mode];
    }

    resetY() {
      this.setYRange(null);
    }

    setHandles(handles) {
      this.handles = handles || [];
      if (this.plot) this.plot.redraw(false, false);
    }

    // Pointer position → { freq, value } in the current mode's units (null outside the plotting area).
    toData(clientX, clientY) {
      if (!this.plot) return null;
      const rect = this.plot.over.getBoundingClientRect();
      const x = clientX - rect.left;
      const y = clientY - rect.top;
      if (x < 0 || y < 0 || x > rect.width || y > rect.height) return null;
      return { freq: this.plot.posToVal(x, 'x'), value: this.plot.posToVal(y, 'y') };
    }

    // CSS-pixel position of (freq, value) relative to the plotting area.
    toPos(freq, value) {
      if (!this.plot) return null;
      return { left: this.plot.valToPos(freq, 'x'), top: this.plot.valToPos(value, 'y') };
    }

    // The handle under the pointer (within `radius` CSS px), nearest first.
    handleAt(clientX, clientY, radius = 14) {
      if (!this.plot || this.mode !== 'magnitude') return null;
      const rect = this.plot.over.getBoundingClientRect();
      let best = null;
      let bestD = radius;
      for (const h of this.handles) {
        const p = this.toPos(h.freq, h.value);
        const d = Math.hypot(clientX - rect.left - p.left, clientY - rect.top - p.top);
        if (d <= bestD) {
          best = h;
          bestD = d;
        }
      }
      return best;
    }

    resize() {
      if (!this.plot) return;
      const width = this.width();
      if (width !== this.plot.width) this.plot.setSize({ width, height: this.height });
    }

    destroy() {
      if (this.resizeObserver) this.resizeObserver.disconnect();
      if (this.plot) this.plot.destroy();
      this.plot = null;
      this.el.replaceChildren();
    }

    // --- internals ---

    width() {
      return Math.max(200, Math.floor(this.el.clientWidth || this.el.getBoundingClientRect().width || 600));
    }

    data() {
      const series = this.traces.map((t) => traceValues(t, this.mode, { unwrap: this.unwrap, grid: this.grid }));
      return [this.grid, ...series];
    }

    yRange() {
      return this.yRanges[this.mode] || defaultRange(this.mode, this.unwrap);
    }

    applyY() {
      if (!this.plot) return;
      const range = this.yRange() || autoRange(this.traces, this.mode, this.grid, this.xRange, { unwrap: this.unwrap });
      this.plot.setScale('y', { min: range[0], max: range[1] });
    }

    build() {
      if (this.plot) this.plot.destroy();
      const mode = this.mode;
      const m = MODES[mode];
      const font = `12px ${cssVar('--font', 'system-ui, sans-serif')}`;
      const axisCommon = {
        stroke: this.colors.axis,
        font,
        grid: { stroke: this.colors.grid, width: 1 },
        ticks: { stroke: this.colors.grid, width: 1, size: 4 },
      };
      const opts = {
        width: this.width(),
        height: this.height,
        title: this.title || undefined,
        legend: { show: false },
        cursor: {
          drag: { x: false, y: false, setScale: false },
          points: { size: 7, width: 1.5 },
        },
        scales: {
          x: { distr: 3, log: 10, auto: false, range: () => this.xRange },
          y: {
            auto: false,
            range: () =>
              this.yRange() || autoRange(this.traces, mode, this.grid, this.xRange, { unwrap: this.unwrap }),
          },
        },
        axes: [
          {
            ...axisCommon,
            splits: (u, i, min, max) => logSplits(min, max),
            filter: (u, splits) => splits, // uPlot's log filter would drop the 2s and 5s
            values: (u, splits) => splits.map((v) => (v === null ? '' : tickLabel(v))),
          },
          {
            ...axisCommon,
            size: 52,
            label: `${m.label} (${m.unit})`,
            labelSize: 18,
            labelFont: font,
            splits: (u, i, min, max) => ySplits(mode, min, max),
            values: (u, splits) => splits.map((v) => (v === null ? '' : yLabel(mode, v))),
          },
        ],
        series: [
          { label: 'Frequency' },
          ...this.traces.map((t) => ({
            label: t.label || t.id,
            stroke: t.color,
            width: t.width ?? (t.bold ? 2.5 : 1.5),
            dash: t.dash,
            show: t.visible !== false,
            spanGaps: false,
            points: { show: false },
          })),
        ],
        hooks: {
          drawAxes: [(u) => this.drawMinorGrid(u)],
          draw: [(u) => this.drawHandles(u)],
          setCursor: [(u) => this.refreshReadout(u.cursor.idx)],
        },
      };
      this.plot = new this.UPlot(opts, this.data(), this.canvasEl);
      this.lastIdx = null;
    }

    drawMinorGrid(u) {
      const { ctx, bbox } = u;
      const [min, max] = [u.scales.x.min, u.scales.x.max];
      ctx.save();
      ctx.strokeStyle = this.colors.grid;
      ctx.globalAlpha = 0.45;
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (const f of minorSplits(min, max)) {
        const x = Math.round(u.valToPos(f, 'x', true)) + 0.5;
        ctx.moveTo(x, bbox.top);
        ctx.lineTo(x, bbox.top + bbox.height);
      }
      ctx.stroke();
      ctx.restore();
    }

    drawHandles(u) {
      if (this.mode !== 'magnitude' || !this.handles.length) return;
      const { ctx } = u;
      const ratio = this.UPlot.pxRatio || 1;
      ctx.save();
      for (const h of this.handles) {
        if (!(h.freq > 0) || !Number.isFinite(h.value)) continue;
        const x = u.valToPos(h.freq, 'x', true);
        const y = u.valToPos(h.value, 'y', true);
        ctx.beginPath();
        ctx.arc(x, y, (h.active ? 7 : 5) * ratio, 0, 2 * Math.PI);
        ctx.fillStyle = h.color || this.colors.text;
        ctx.globalAlpha = h.disabled ? 0.35 : 0.9;
        ctx.fill();
        ctx.lineWidth = (h.active ? 2 : 1) * ratio;
        ctx.strokeStyle = this.colors.text;
        ctx.stroke();
      }
      ctx.restore();
    }

    refreshReadout(idx = this.lastIdx) {
      this.lastIdx = idx;
      const readout = idx === null || idx === undefined ? null : readoutRows(this.traces, this.grid, idx);
      this.renderReadout(readout);
      if (this.onCursor) this.onCursor(readout);
    }

    renderReadout(readout) {
      const el = this.readoutEl;
      if (!el) return;
      if (!readout) {
        el.replaceChildren(textEl('span', 'graph-readout-hint', 'Hover the graph for values'));
        return;
      }
      const head = textEl('div', 'graph-readout-freq', fmtFreq(readout.freq));
      const list = document.createElement('ul');
      list.className = 'graph-readout-rows';
      for (const r of readout.rows) {
        const li = document.createElement('li');
        const swatch = textEl('span', 'graph-swatch', '');
        swatch.style.background = r.color;
        swatch.setAttribute('aria-hidden', 'true');
        li.append(swatch, textEl('span', 'graph-readout-label', r.label || r.id));
        li.append(textEl('span', 'graph-readout-db', fmtDb(r.db)));
        li.append(textEl('span', 'graph-readout-phase', fmtPhase(r.phase)));
        if (this.mode === 'group_delay') li.append(textEl('span', 'graph-readout-gd', fmtMs(r.group_delay)));
        list.appendChild(li);
      }
      el.replaceChildren(head, list);
    }
  }

  function textEl(tag, className, text) {
    const e = document.createElement(tag);
    e.className = className;
    e.textContent = text;
    return e;
  }

  const create = (el, opts) => new ResponsePlot(el, opts);

  const api = Object.freeze({
    VIEWS,
    MODES,
    COLORS,
    DEFAULT_DB_RANGE,
    channelColor,
    fmtFreq,
    fmtDb,
    fmtPhase,
    fmtMs,
    tickLabel,
    logSplits,
    minorSplits,
    nearestIndex,
    resample,
    traceValues,
    readoutRows,
    autoRange,
    defaultRange,
    ySplits,
    ResponsePlot,
    create,
  });

  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.mdsp = Object.assign(window.mdsp || {}, { graph: api });
})();
