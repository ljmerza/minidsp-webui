// Response graphs on uPlot (docs 05 "Graph details"): log-frequency x axis with 1-2-5 decade ticks, magnitude / phase /
// group-delay modes, a hover readout of f, dB and phase for every trace, and fixed per-channel colours.
//
//   const plot = window.mdsp.graph.create(el, { view: 'sub', mode: 'magnitude' });
//   (view: 'sub' | 'full' | [fMin, fMax]; dbRange: the magnitude scale, by default ± Settings' default graph range)
//   plot.setTraces([{ id: 'o1', label: 'Shakers', color: '#f0b429', response }, ...]);
//
// A trace's `response` is the server's Response.to_dict() or a window.mdsp.dsp response (same keys: freqs,
// magnitude_db, phase_deg, phase_unwrapped_deg, group_delay_ms). Traces on other grids are resampled onto the first
// trace's. Optional per trace: width (px), dash ([on, off] px), bold (composite curve), visible.
//
// Handles (PEQ editors): plot.setHandles([{ id, freq, value, color, active }]) draws them in magnitude mode;
// handleAt(clientX, clientY) and toData(clientX, clientY) map pointer events back to bands and to (freq, dB).
//
// Phase 9 (design §12.3, §16 a11y 9 and phone 7, P09): every plot has an HTML legend built from the trace metadata
// (opts.legend === false leaves it out), and a cursor you can place: a click or tap pins it, the arrow keys move it
// when the plot has focus (it is a slider in frequency), Esc removes it. The readout below the plot is a fixed text
// box (never a floating tooltip) showing the hovered point, else the pinned one. classifyNotes() sorts a graph's
// warnings into real warnings and rounding notes, with the stronger wording at ROUNDING_STRONG_DB or more.
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

  // --- legend, cursor and notes (phase 9) ---------------------------------------------------------------------

  // The legend rows of the visible traces, in plot order: { id, label, color, style: 'bold' | 'dashed' | 'solid' }.
  // Traces sharing a label and colour (the integration plot's wrap segments) show once; `legend: false` hides one.
  function legendItems(traces) {
    const seen = new Set();
    const out = [];
    for (const t of traces || []) {
      if (t.visible === false || t.legend === false) continue;
      const label = t.label || t.id;
      if (!label) continue;
      const color = t.color || COLORS.fallback;
      const key = `${label}\u0000${color}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const style = Array.isArray(t.dash) && t.dash.length ? 'dashed' : t.bold ? 'bold' : 'solid';
      out.push({ id: t.id ?? label, label, color, style });
    }
    return out;
  }

  // Rounding notes at or above this many dB get the stronger wording (design §1 grafts, glossary "rounding").
  const ROUNDING_STRONG_DB = 1;
  const ROUNDING_RE = /^(.*?):\s*rounding note\b.*?\bup to (\d+(?:\.\d+)?) dB/;

  // "output 1: rounding note: … by up to 2.29 dB" → { label: 'output 1', db: 2.29 }; null for any other warning.
  function parseRoundingNote(text) {
    const m = ROUNDING_RE.exec(String(text || ''));
    if (!m) return null;
    const db = Number(m[2]);
    return Number.isFinite(db) ? { label: m[1].trim(), db } : null;
  }

  // A graph's warnings split into real warnings (`others`, shown as they are) and rounding notes (folded, grey,
  // stronger wording at `strongDb` or more). `summary` is the folded line: "1 rounding note (0.20 dB, harmless)",
  // "1 rounding note: this filter's real curve is noticeably off (2.3 dB)", "3 rounding notes (up to 0.20 dB,
  // harmless)", "3 rounding notes: one filter's real curve is noticeably off (2.3 dB)".
  function classifyNotes(warnings, strongDb = ROUNDING_STRONG_DB) {
    const others = [];
    const notes = [];
    for (const w of warnings || []) {
      const note = parseRoundingNote(w);
      if (!note) {
        others.push(w);
        continue;
      }
      const strong = note.db >= strongDb;
      const text = strong
        ? `${note.label}: this filter's real curve is noticeably off (${note.db.toFixed(1)} dB)`
        : `${note.label}: off by up to ${note.db.toFixed(2)} dB, harmless`;
      notes.push({ ...note, strong, text });
    }
    const worst = notes.reduce((m, n) => Math.max(m, n.db), 0);
    const nStrong = notes.filter((n) => n.strong).length;
    let summary = '';
    if (notes.length === 1) {
      summary = nStrong
        ? `1 rounding note: this filter's real curve is noticeably off (${worst.toFixed(1)} dB)`
        : `1 rounding note (${worst.toFixed(2)} dB, harmless)`;
    } else if (notes.length > 1 && nStrong === 1) {
      summary = `${notes.length} rounding notes: one filter's real curve is noticeably off (${worst.toFixed(1)} dB)`;
    } else if (notes.length > 1 && nStrong > 1) {
      summary =
        `${notes.length} rounding notes: ${nStrong} filters' real curves are noticeably off ` +
        `(up to ${worst.toFixed(1)} dB)`;
    } else if (notes.length > 1) {
      summary = `${notes.length} rounding notes (up to ${worst.toFixed(2)} dB, harmless)`;
    }
    return { others, notes, strong: nStrong > 0, summary };
  }

  // The readout as one line of text (the slider's aria-valuetext): "50.0 Hz: Shakers −0.1 dB, Sub −3.2 dB".
  function readingText(readout, mode = 'magnitude') {
    if (!readout) return '';
    const value = (r) => {
      if (mode === 'phase') return fmtPhase(r.phase);
      if (mode === 'group_delay') return fmtMs(r.group_delay);
      return fmtDb(r.db);
    };
    const rows = readout.rows.map((r) => `${r.label || r.id} ${value(r)}`);
    return rows.length ? `${fmtFreq(readout.freq)}: ${rows.join(', ')}` : fmtFreq(readout.freq);
  }

  // Cursor steps on a log grid: a key press moves about 1/12 octave (`big`: half an octave), and always at least one
  // point; the result stays inside [lo, hi] (grid indexes of the visible range).
  const KEY_STEP_OCT = 1 / 12;
  const BIG_STEP_OCT = 1 / 2;
  function stepIndex(grid, idx, direction, { big = false, lo = 0, hi = grid.length - 1 } = {}) {
    if (!grid.length) return null;
    const dir = direction < 0 ? -1 : 1;
    const from = Math.min(hi, Math.max(lo, idx));
    const target = grid[from] * 2 ** (dir * (big ? BIG_STEP_OCT : KEY_STEP_OCT));
    let next = nearestIndex(grid, target);
    if (next === from) next = from + dir;
    return Math.min(hi, Math.max(lo, next));
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
:where(.graph-readout-label) { color: var(--text-muted, #8c96a3); }
:where(.graph-legend) { display: flex; flex-wrap: wrap; gap: 0.2rem 1rem; list-style: none; margin: 0 0 0.3rem; padding: 0;
  font-size: 0.82rem; }
:where(.graph-legend li) { display: inline-flex; gap: 0.4rem; align-items: center; }
:where(.graph-legend-line) { display: inline-block; width: 1.4rem; height: 0; border-top: 2px solid currentColor; }
:where(.graph-legend-line.is-dashed) { border-top-style: dashed; }
:where(.graph-legend-line.is-bold) { border-top-width: 4px; }
:where(.graph-canvas[role="slider"]) { cursor: crosshair; }
:where(.graph-notes) { margin-top: 0.3rem; font-size: 0.82rem; }
:where(.graph-rounding) { display: flex; align-items: center; gap: 0.25rem; flex-wrap: wrap; color: var(--text-muted, #8c96a3); }
:where(.graph-rounding.is-strong) { color: var(--warn, #f0b429); }
:where(.graph-rounding details) { flex: 1 1 0; min-width: 0; }
:where(.graph-rounding summary) { cursor: pointer; }
:where(.graph-rounding ul) { margin: 0.2rem 0 0; padding-left: 1.2rem; }
:where(.graph-reading) { margin: 0.2rem 0; }`;

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

  // Settings' default graph range (docs 05 §9), rendered on <body data-graph-view data-graph-db>. No view there means
  // every page keeps its own default.
  function pageDefaults() {
    const ds = (typeof document !== 'undefined' && document.body && document.body.dataset) || {};
    const db = Number(ds.graphDb);
    return {
      view: VIEWS[ds.graphView] ? ds.graphView : null,
      dbRange: ds.graphDb && Number.isFinite(db) && db > 0 ? [-db, db] : [...DEFAULT_DB_RANGE],
    };
  }

  // The named view a graph opens in when the user hasn't picked one on that page: Settings', else the page's own.
  const defaultView = (pageView) => pageDefaults().view || (VIEWS[pageView] ? pageView : DEFAULT_VIEW);

  const validRange = (r) => Array.isArray(r) && r.length === 2 && Number.isFinite(r[0]) && r[1] > r[0];

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
      this.dbRange = validRange(opts.dbRange) ? [opts.dbRange[0], opts.dbRange[1]] : pageDefaults().dbRange;
      this.height = opts.height || 260;
      this.traces = [];
      this.grid = [];
      this.handles = [];
      this.onCursor = opts.onCursor || null;
      this.title = opts.title || '';
      this.signature = null;
      this.plot = null;

      this.pinFreq = null; // the placed cursor's frequency (kept across rebuilds and grid changes)
      this.label = opts.label || 'Graph';
      this.valueTimer = null;

      injectDefaultCss();
      el.classList.add('graph');
      if (opts.legend !== false) {
        this.legendEl = document.createElement('ul');
        this.legendEl.className = 'graph-legend';
        this.legendEl.setAttribute('aria-label', 'Legend');
        el.appendChild(this.legendEl);
      } else {
        this.legendEl = null;
      }
      this.canvasEl = document.createElement('div');
      this.canvasEl.className = 'graph-canvas';
      el.appendChild(this.canvasEl);
      this.bindCursor();
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
      this.updateSlider();
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
      this.renderLegend();
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
      this.updateSlider();
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

    // --- the placed cursor ---

    // Grid index of the placed cursor, or null (none placed, or outside the visible range).
    pinnedIndex() {
      if (this.pinFreq === null || !this.grid.length) return null;
      const [lo, hi] = this.visibleIndexRange();
      if (lo > hi) return null;
      const idx = nearestIndex(this.grid, this.pinFreq);
      return idx < lo || idx > hi ? null : idx;
    }

    // First and last grid index inside the x range.
    visibleIndexRange() {
      const [fMin, fMax] = this.xRange;
      let lo = 0;
      let hi = this.grid.length - 1;
      while (lo <= hi && this.grid[lo] < fMin * (1 - 1e-9)) lo++;
      while (hi >= lo && this.grid[hi] > fMax * (1 + 1e-9)) hi--;
      return [lo, hi];
    }

    // Place the cursor at a frequency (null removes it). The readout and the slider value follow.
    pin(freq) {
      this.pinFreq = freq === null || freq === undefined || !(freq > 0) ? null : freq;
      if (this.plot) this.plot.redraw(false, false);
      this.refreshReadout(null);
    }

    // A click or tap inside the plotting area places the cursor there (on a PEQ handle it also selects the band).
    pinAt(clientX, clientY) {
      const d = this.toData(clientX, clientY);
      if (!d || !this.grid.length) return;
      this.pin(this.grid[nearestIndex(this.grid, d.freq)]);
    }

    bindCursor() {
      const c = this.canvasEl;
      if (!c || typeof c.addEventListener !== 'function') return;
      c.setAttribute('tabindex', '0');
      c.setAttribute('role', 'slider');
      c.setAttribute('aria-label', `${this.label}: cursor (arrow keys move it, Esc removes it)`);
      c.setAttribute('aria-orientation', 'horizontal');
      c.addEventListener('click', (e) => this.pinAt(e.clientX, e.clientY));
      c.addEventListener('keydown', (e) => this.onKey(e));
    }

    // Arrow keys move the cursor about 1/12 octave (Shift or PageUp/PageDown: half an octave), Home/End jump to the
    // ends, Esc removes it. The first key press places it in the middle of the view.
    onKey(e) {
      const keys = {
        ArrowRight: [1, false],
        ArrowUp: [1, false],
        ArrowLeft: [-1, false],
        ArrowDown: [-1, false],
        PageUp: [1, true],
        PageDown: [-1, true],
      };
      if (!this.grid.length) return;
      const [lo, hi] = this.visibleIndexRange();
      if (lo > hi) return;
      let idx = null;
      const current = this.pinnedIndex();
      if (e.key === 'Escape') {
        if (current === null) return;
        e.preventDefault();
        this.pin(null);
        return;
      }
      if (e.key === 'Home') idx = lo;
      else if (e.key === 'End') idx = hi;
      else if (keys[e.key]) {
        const [dir, big] = keys[e.key];
        if (current === null) idx = nearestIndex(this.grid, Math.sqrt(this.xRange[0] * this.xRange[1]));
        else idx = stepIndex(this.grid, current, dir, { big: big || e.shiftKey, lo, hi });
      } else {
        return;
      }
      e.preventDefault();
      this.pin(this.grid[idx]);
    }

    // The slider's value (Hz) and its spoken text, the one-line reading. The text is throttled (at most every
    // 300 ms, the last one always lands) so holding an arrow key doesn't queue a long list of announcements.
    updateSlider(readout = null) {
      const c = this.canvasEl;
      if (!c || typeof c.setAttribute !== 'function') return;
      const [fMin, fMax] = this.xRange;
      c.setAttribute('aria-valuemin', String(Math.round(fMin)));
      c.setAttribute('aria-valuemax', String(Math.round(fMax)));
      const idx = this.pinnedIndex();
      const freq = idx === null ? fMin : this.grid[idx];
      c.setAttribute('aria-valuenow', String(Math.round(freq)));
      const text = idx === null ? 'no cursor placed' : readingText(readout, this.mode) || fmtFreq(freq);
      if (this.valueText === text) return;
      this.valueText = text;
      clearTimeout(this.valueTimer);
      if (idx === null) {
        this.valueAt = 0;
        c.setAttribute('aria-valuetext', text);
        return;
      }
      const now = Date.now();
      const apply = () => {
        this.valueAt = Date.now();
        c.setAttribute('aria-valuetext', this.valueText);
      };
      if (!this.valueAt || now - this.valueAt >= 300) apply();
      else this.valueTimer = setTimeout(apply, 300 - (now - this.valueAt));
    }

    renderLegend() {
      const el = this.legendEl;
      if (!el) return;
      const items = legendItems(this.traces);
      el.hidden = items.length === 0;
      el.replaceChildren(
        ...items.map((item) => {
          const li = document.createElement('li');
          const line = textEl('span', `graph-legend-line is-${item.style}`, '');
          line.style.color = item.color;
          line.setAttribute('aria-hidden', 'true');
          li.append(line, textEl('span', 'graph-legend-label', item.label));
          return li;
        }),
      );
    }

    resize() {
      if (!this.plot) return;
      const width = this.width();
      if (width !== this.plot.width) this.plot.setSize({ width, height: this.height });
    }

    destroy() {
      clearTimeout(this.valueTimer);
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
      if (this.yRanges[this.mode]) return this.yRanges[this.mode];
      return this.mode === 'magnitude' ? [...this.dbRange] : defaultRange(this.mode, this.unwrap);
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
          draw: [(u) => this.drawPinned(u), (u) => this.drawHandles(u)],
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

    // The placed cursor: a dashed vertical line and a dot on each visible trace.
    drawPinned(u) {
      const idx = this.pinnedIndex();
      if (idx === null) return;
      const { ctx, bbox } = u;
      const ratio = this.UPlot.pxRatio || 1;
      const x = Math.round(u.valToPos(this.grid[idx], 'x', true)) + 0.5;
      ctx.save();
      ctx.strokeStyle = this.colors.text;
      ctx.globalAlpha = 0.7;
      ctx.lineWidth = ratio;
      if (ctx.setLineDash) ctx.setLineDash([4 * ratio, 3 * ratio]);
      ctx.beginPath();
      ctx.moveTo(x, bbox.top);
      ctx.lineTo(x, bbox.top + bbox.height);
      ctx.stroke();
      if (ctx.setLineDash) ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      const data = this.plot === u ? u.data : null;
      this.traces.forEach((t, k) => {
        if (t.visible === false || !data) return;
        const v = data[k + 1] && data[k + 1][idx];
        if (v === null || v === undefined || !Number.isFinite(v)) return;
        const [ymin, ymax] = [u.scales.y.min, u.scales.y.max];
        if (v < ymin || v > ymax) return;
        ctx.beginPath();
        ctx.arc(x, u.valToPos(v, 'y', true), 4 * ratio, 0, 2 * Math.PI);
        ctx.fillStyle = t.color;
        ctx.fill();
      });
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

    // The hovered point (`idx`, from uPlot's cursor), else the placed cursor.
    refreshReadout(idx = this.lastIdx) {
      this.lastIdx = idx;
      const hovered = idx !== null && idx !== undefined && idx >= 0;
      const pinned = this.pinnedIndex();
      const at = hovered ? idx : pinned;
      const readout = at === null || at === undefined ? null : readoutRows(this.traces, this.grid, at);
      this.renderReadout(readout);
      this.updateSlider(pinned === null ? null : hovered ? readoutRows(this.traces, this.grid, pinned) : readout);
      if (this.onCursor) this.onCursor(readout);
    }

    renderReadout(readout) {
      const el = this.readoutEl;
      if (!el) return;
      if (!readout) {
        el.replaceChildren(textEl('span', 'graph-readout-hint', 'Tap or click the graph for values'));
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
    pageDefaults,
    defaultView,
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
    legendItems,
    ROUNDING_STRONG_DB,
    parseRoundingNote,
    classifyNotes,
    readingText,
    stepIndex,
    autoRange,
    defaultRange,
    ySplits,
    ResponsePlot,
    create,
  });

  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.mdsp = Object.assign(window.mdsp || {}, { graph: api });
})();
