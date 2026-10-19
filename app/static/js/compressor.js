// Output compressor tab (docs 05 §4): enable, threshold/ratio/attack/release, presets and the static transfer curve
// (docs 04 §6, hard knee, approximate). Talks to the JSON API (app/api/v1/compressor_fir.py).
//
// Typing redraws the curve locally; a committed field goes to the server, which validates it and queues the write.
// A refused value puts the stored one back. Sync badges follow "sync" events, "desired" events from other tabs reload.
(() => {
  'use strict';

  const FIELDS = ['threshold_db', 'ratio', 'attack_ms', 'release_ms'];
  const LABELS = { threshold_db: 'Threshold (dBFS)', ratio: 'Ratio', attack_ms: 'Attack (ms)', release_ms: 'Release (ms)' };
  const ICON = { synced: '✓', pending: '⟳', failed: '⚠', unknown: '?' };
  const RANK = { synced: 0, unknown: 1, pending: 2, failed: 3 };
  const CURVE_LO = -90;
  const CURVE_HI = 0;
  const RELOAD_DELAY_MS = 350;
  const THRESHOLD_STEP_DB = 1; // the − / + steppers beside the threshold
  const STEP_COMMIT_MS = 400; // quick stepper presses are sent once

  // The signed-text parser shared with the PEQ editor (peq_editor.js, loaded first on this tab).
  const filters = () => (window.mdsp && window.mdsp.filters) || {};

  const toast = (message, level) => window.mdsp && window.mdsp.toast && window.mdsp.toast(message, level);

  const MINUS = '\u2212';
  const num = (v) => {
    const n = Math.round(Number(v) * 10) / 10;
    return n < 0 ? `${MINUS}${-n}` : `${n}`;
  };

  // The tab's plain-language line (phase 9 design §8.2): "Above −36 dBFS, every 4 dB louder comes out only 1 dB
  // louder."
  function inWords(threshold, ratio, bypass) {
    if (bypass) return 'Off: this output passes through unchanged.';
    const r = Number(ratio);
    if (!(r > 1)) return `Ratio 1:1 changes nothing, even above ${num(threshold)} dBFS.`;
    const text = `Above ${num(threshold)} dBFS, every ${num(r)} dB louder comes out only 1 dB louder.`;
    return r >= 20 ? `${text} At this ratio it acts as a limiter.` : text;
  }

  // A template button: "Shaker protection (−36, 50:1, 15, 30)".
  function presetText(preset) {
    const v = (preset && preset.values) || {};
    if (!('threshold_db' in v)) return (preset && preset.label) || '';
    return `${preset.label} (${num(v.threshold_db)}, ${num(v.ratio)}:1, ${num(v.attack_ms)}, ${num(v.release_ms)})`;
  }

  // Hard-knee static curve: out = in up to the threshold, T + (in - T) / ratio above. Ratios below 1 draw as 1:1.
  function transferCurve(threshold, ratio, lo = CURVE_LO, hi = CURVE_HI, step = 1) {
    const r = Math.max(Number(ratio) || 1, 1);
    const t = Number(threshold);
    const xs = [];
    for (let x = lo; x <= hi + 1e-9; x += step) xs.push(Math.round(x * 1000) / 1000);
    if (t > lo && t < hi && !xs.includes(t)) {
      xs.push(t);
      xs.sort((a, b) => a - b);
    }
    return xs.map((x) => [x, x <= t ? x : Math.round((t + (x - t) / r) * 1000) / 1000]);
  }

  function worst(statuses) {
    let w = null;
    for (const st of statuses) if (w === null || RANK[st] > RANK[w]) w = st;
    return w || 'unknown';
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

  async function request(url, { method = 'GET', body } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin',
      });
    } catch {
      return { ok: false, status: 0, data: { detail: 'the web UI server is unreachable' } };
    }
    const data = await response.json().catch(() => null);
    return { ok: response.ok, status: response.status, data };
  }

  function errorText(res) {
    const d = res.data || {};
    const detail = typeof d.detail === 'string' ? d.detail : null; // already lists the problems
    return detail || (d.problems || []).join('; ') || `Change refused (HTTP ${res.status})`;
  }

  const withQuery = (url, query) => `${url}${url.includes('?') ? '&' : '?'}${query}`;

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function createCurvePlot(el, height) {
    if (!window.uPlot || !el) return null;
    const axis = cssVar('--text-muted', '#8c96a3');
    const grid = cssVar('--border', '#2a313b');
    const font = `12px ${cssVar('--font', 'system-ui, sans-serif')}`;
    const common = { stroke: axis, font, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1, size: 4 } };
    const width = () => Math.max(240, Math.min(el.clientWidth || 480, 640));
    const plot = new window.uPlot(
      {
        width: width(),
        height,
        legend: { show: false },
        cursor: { drag: { x: false, y: false, setScale: false } },
        scales: { x: { time: false, auto: false, range: [CURVE_LO, CURVE_HI] }, y: { auto: false, range: [CURVE_LO, CURVE_HI] } },
        axes: [
          { ...common, label: 'Input (dBFS)', labelFont: font, labelSize: 18 },
          { ...common, size: 48, label: 'Output (dBFS)', labelFont: font, labelSize: 18 },
        ],
        series: [
          { label: 'Input' },
          { label: '1:1', stroke: grid, width: 1, dash: [4, 4] },
          { label: 'Output', stroke: cssVar('--accent', '#4ea1ff'), width: 2.5 },
        ],
      },
      [[], [], []],
      el,
    );
    let observer = null;
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => plot.setSize({ width: width(), height }));
      observer.observe(el);
    }
    return {
      plot,
      setCurve(points) {
        const xs = points.map((p) => p[0]);
        plot.setData([xs, xs.slice(), points.map((p) => p[1])]);
      },
      destroy() {
        if (observer) observer.disconnect();
        plot.destroy();
      },
    };
  }

  function compressorEditor(cfg) {
    const ctl = { plot: null, reloadTimer: null, stepTimer: null, stepTarget: null };

    return {
      cfg,
      loaded: false,
      server: null,
      form: { threshold_db: 0, ratio: 1, attack_ms: 10, release_ms: 100 },
      // The threshold is signed, so its field is text (phase 9 P01): a number field plus x-model.number wiped a typed
      // "-", and "-10" arrived as 10. Parsed on every keystroke, committed on change only when it's a whole number.
      thresholdText: '0',
      bypass: true,
      sync: {},
      presets: {},
      ranges: {},
      notes: [],
      error: null,
      inflight: false,
      prefix: `outputs.${cfg.channel}.compressor`,

      init() {
        this.$el.addEventListener('sse:sync', (e) => this.onSync(e));
        this.$el.addEventListener('sse:desired', (e) => this.onDesired(e));
        this.$nextTick(() => {
          ctl.plot = createCurvePlot(this.$refs.graph, cfg.height || 260);
          this.load();
        });
      },

      destroy() {
        if (ctl.plot) ctl.plot.destroy();
      },

      async load() {
        const res = await request(cfg.url);
        if (!res.ok) {
          this.error = errorText(res);
          return;
        }
        this.error = null;
        this.apply(res.data);
      },

      apply(view) {
        this.server = view.compressor;
        this.form = Object.fromEntries(FIELDS.map((k) => [k, view.compressor[k]]));
        if (ctl.stepTarget !== null) this.form.threshold_db = ctl.stepTarget; // stepper presses not sent yet win
        this.syncThresholdText();
        this.bypass = view.compressor.bypass;
        this.sync = { ...view.sync_status };
        this.presets = view.presets || this.presets;
        this.ranges = view.ranges || this.ranges;
        this.notes = view.notes || this.notes;
        this.loaded = true;
        this.render();
      },

      onSync(e) {
        const data = parseEvent(e);
        if (!data || data.preset !== cfg.preset) return;
        const next = { ...this.sync };
        for (const [path, st] of Object.entries(data.changes || {})) if (path.startsWith(`${this.prefix}.`)) next[path] = st;
        for (const path of data.removed || []) delete next[path];
        this.sync = next;
      },

      onDesired(e) {
        const data = parseEvent(e);
        if (!data || data.preset !== cfg.preset || this.inflight) return;
        if (!(data.paths || []).some((p) => p.startsWith(`${this.prefix}.`))) return;
        clearTimeout(ctl.reloadTimer);
        ctl.reloadTimer = setTimeout(() => this.load(), RELOAD_DELAY_MS);
      },

      // --- view helpers ----------------------------------------------------------------------------------------

      status(key) {
        const paths = key ? [`${this.prefix}.${key}`] : Object.keys(this.sync);
        const statuses = paths.filter((p) => p in this.sync).map((p) => this.sync[p]);
        return worst(statuses);
      },
      icon(key) {
        return ICON[this.status(key)];
      },
      range(key) {
        return this.ranges[key] || [null, null];
      },
      invalid(key) {
        const v = Number(this.form[key]);
        const [lo, hi] = this.range(key);
        return this.form[key] === '' || !Number.isFinite(v) || (lo !== null && (v < lo || v > hi));
      },
      reduction() {
        // Gain reduction at 0 dBFS input, the loudest the static curve can see.
        const t = Number(this.form.threshold_db);
        const r = Math.max(Number(this.form.ratio) || 1, 1);
        return t >= 0 ? 0 : -t * (1 - 1 / r);
      },
      inWords() {
        return inWords(this.form.threshold_db, this.form.ratio, this.bypass);
      },
      presetText(preset) {
        return presetText(preset);
      },
      ratioText() {
        const r = Number(this.form.ratio);
        return r >= 50 ? `${r}:1 (limiter)` : `${r}:1`;
      },

      render() {
        if (ctl.plot) ctl.plot.setCurve(transferCurve(this.form.threshold_db, this.form.ratio));
      },

      // --- edits -----------------------------------------------------------------------------------------------

      async put(body) {
        this.inflight = true;
        let res = await request(cfg.url, { method: 'PUT', body });
        if (res.status === 409 && res.data && res.data.error === 'confirmation_required') {
          if (window.confirm(`${res.data.detail}.\n\nApply it anyway?`)) {
            res = await request(withQuery(cfg.url, 'confirm=true'), { method: 'PUT', body });
          } else {
            res = { ...res, cancelled: true };
          }
        }
        this.inflight = false;
        if (res.cancelled) {
          this.revert();
          return;
        }
        if (!res.ok) {
          this.error = errorText(res);
          toast(this.error);
          this.revert();
          return;
        }
        this.error = null;
        this.apply(res.data.view);
        for (const [path, st] of Object.entries(res.data.sync || {})) this.sync[path] = st;
      },

      revert() {
        if (this.server) {
          this.form = Object.fromEntries(FIELDS.map((k) => [k, this.server[k]]));
          this.bypass = this.server.bypass;
        }
        this.syncThresholdText();
        this.render();
      },

      commit(key) {
        if (this.invalid(key)) {
          const [lo, hi] = this.range(key);
          toast(`${LABELS[key]} must be between ${lo} and ${hi}`);
          this.revert();
          return;
        }
        const value = Number(this.form[key]);
        if (this.server && this.server[key] === value) return;
        this.put({ [key]: value });
      },

      setEnabled(on) {
        this.bypass = !on;
        this.put({ bypass: !on });
      },

      applyPreset(key) {
        this.put({ preset: key, bypass: false });
      },

      // --- threshold (signed text field + steppers) ----------------------------------------------------------------

      syncThresholdText() {
        this.thresholdText = filters().fieldText ? filters().fieldText(this.form.threshold_db) : String(this.form.threshold_db);
      },

      parseThreshold() {
        const parse = filters().parseSigned;
        if (parse) return parse(this.thresholdText);
        const v = Number(this.thresholdText);
        return Number.isFinite(v) && String(this.thresholdText).trim() !== '' ? { kind: 'number', value: v } : { kind: 'invalid' };
      },

      // Flags text that can't become a threshold: not a number, or out of range. A half-typed "-" isn't flagged.
      thresholdInvalid() {
        const parsed = this.parseThreshold();
        if (parsed.kind === 'partial') return false;
        if (parsed.kind !== 'number') return true;
        const [lo, hi] = this.range('threshold_db');
        return (lo !== null && parsed.value < lo) || (hi !== null && parsed.value > hi);
      },

      // While typing: a whole number moves the curve; a half-typed "-" or "." changes nothing.
      onThresholdInput() {
        const parsed = this.parseThreshold();
        if (parsed.kind !== 'number') return;
        this.form.threshold_db = parsed.value;
        this.render();
      },

      // Change/blur: refused (and the stored value put back) unless the text is a whole number in range.
      commitThreshold() {
        const parsed = this.parseThreshold();
        if (parsed.kind !== 'number') {
          const [lo, hi] = this.range('threshold_db');
          toast(`${LABELS.threshold_db} must be a number between ${lo} and ${hi}`);
          this.revert();
          return;
        }
        this.form.threshold_db = parsed.value;
        this.commit('threshold_db');
        if (!this.invalid('threshold_db')) this.syncThresholdText();
      },

      // The slider moved: the text field follows.
      onThresholdSlide() {
        this.syncThresholdText();
        this.render();
      },

      canStep(direction) {
        const [lo, hi] = this.range('threshold_db');
        const v = Number(this.form.threshold_db);
        if (!this.loaded || !Number.isFinite(v)) return false;
        return direction > 0 ? hi === null || v < hi : lo === null || v > lo;
      },

      // − / +: 1 dB steps inside the range; quick presses are sent once (no minus key needed on a phone).
      stepThreshold(direction) {
        if (!this.canStep(direction)) return;
        const [lo, hi] = this.range('threshold_db');
        let v = Math.round((Number(this.form.threshold_db) + direction * THRESHOLD_STEP_DB) * 2) / 2;
        if (lo !== null) v = Math.max(lo, v);
        if (hi !== null) v = Math.min(hi, v);
        ctl.stepTarget = v;
        this.form.threshold_db = v;
        this.syncThresholdText();
        this.render();
        this.scheduleStep();
      },

      scheduleStep() {
        clearTimeout(ctl.stepTimer);
        ctl.stepTimer = setTimeout(() => this.flushStep(), cfg.stepDelayMs ?? STEP_COMMIT_MS);
      },

      // Sends the stepped threshold; waits while an earlier request is on its way, so replies can't arrive out of order.
      flushStep() {
        if (ctl.stepTarget === null) return;
        if (this.inflight) {
          this.scheduleStep();
          return;
        }
        this.form.threshold_db = ctl.stepTarget;
        ctl.stepTarget = null;
        this.syncThresholdText();
        this.commit('threshold_db');
      },
    };
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = { transferCurve, inWords, presetText };
  if (typeof window === 'undefined') return;
  window.mdsp = Object.assign(window.mdsp || {}, { compressor: { transferCurve, inWords, presetText } });
  document.addEventListener('alpine:init', () => {
    window.Alpine.data('compressorEditor', compressorEditor);
  });
})();
